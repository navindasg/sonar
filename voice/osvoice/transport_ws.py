"""WebSocket transport: bridge one connection to a `Pipeline`.

A single ``/ws`` connection runs a small `asyncio.TaskGroup`:

* one READER awaits ``ws.receive()`` — binary frames are PCM16 mic audio pushed
  into the pipeline's per-turn audio stream; text frames are JSON control
  messages (``config`` / ``interrupt``);
* one WRITER, and ONLY this task, calls ``ws.send_*`` — interleaved sends from
  multiple tasks corrupt WebSocket frames. The writer drains a single merged
  outbound queue carrying both JSON events and binary audio.

Pipeline events arrive through the injected ``emit`` callback (it enqueues a JSON
item); a pump task moves PCM frames off the pipeline's audio queue onto the same
merged queue. Disconnects tear down the whole group cleanly.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from osvoice.pipeline import Pipeline, PipelineEvent

if TYPE_CHECKING:  # FastAPI/Starlette types for checkers only.
    from fastapi import WebSocket

logger = logging.getLogger("osvoice.transport.ws")

# Sentinel pushed onto the merged queue to ask the writer to stop.
_STOP: Any = object()

PipelineFactory = Callable[["EmitFn"], Pipeline]
EmitFn = Callable[[PipelineEvent], Awaitable[None]]


class _AudioStream:
    """A restartable async byte stream feeding STT for the current turn.

    The reader pushes PCM frames; `frames()` yields them until `end_turn()` is
    called (turn endpointed) at which point the iterator stops, letting STT emit
    its final transcript. A fresh internal queue starts each turn.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def push(self, frame: bytes) -> None:
        """Enqueue one PCM16 frame for the in-progress turn."""
        self._queue.put_nowait(frame)

    def end_turn(self) -> None:
        """Signal end-of-utterance so the current `frames()` iterator completes."""
        self._queue.put_nowait(None)

    def reset(self) -> None:
        """Start a fresh queue for the next turn (drops any stragglers)."""
        self._queue = asyncio.Queue()

    async def frames(self) -> AsyncIterator[bytes]:
        """Yield PCM frames until an end-of-turn sentinel arrives."""
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame


class _Connection:
    """Owns the per-connection state and the reader/writer/turn tasks."""

    def __init__(self, ws: WebSocket, make_pipeline: PipelineFactory) -> None:
        self._ws = ws
        self._merged: asyncio.Queue[Any] = asyncio.Queue()
        self._audio = _AudioStream()
        self._pipeline = make_pipeline(self._emit)
        self._turn_task: asyncio.Task[None] | None = None

    async def _emit(self, event: PipelineEvent) -> None:
        """Pipeline event callback: enqueue a JSON item for the sole writer."""
        await self._merged.put({"type": event.kind, "text": event.text})

    async def run(self) -> None:
        """Run reader, writer and audio pump together until the socket closes."""
        await self._ws.accept()
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._writer(), name="ws-writer")
                group.create_task(self._audio_pump(), name="ws-audio-pump")
                group.create_task(self._reader(group), name="ws-reader")
        except* _Disconnect:
            logger.info("websocket disconnected; connection torn down")
        finally:
            await self._pipeline_aclose()

    async def _reader(self, group: asyncio.TaskGroup) -> None:
        """Receive frames; dispatch audio vs. control until disconnect."""
        from starlette.websockets import WebSocketDisconnect

        try:
            while True:
                message = await self._ws.receive()
                if message.get("type") == "websocket.disconnect":
                    raise _Disconnect()
                await self._dispatch(message, group)
        except WebSocketDisconnect as exc:
            raise _Disconnect() from exc
        finally:
            await self._merged.put(_STOP)

    async def _dispatch(self, message: dict, group: asyncio.TaskGroup) -> None:
        """Route one received message to audio input or control handling."""
        if (data := message.get("bytes")) is not None:
            self._audio.push(data)
        elif (raw := message.get("text")) is not None:
            await self._control(raw, group)

    async def _control(self, raw: str, group: asyncio.TaskGroup) -> None:
        """Handle a JSON control message: start/end turn, config or interrupt."""
        action = _parse_control(raw)
        if action == "interrupt":
            await self._pipeline.barge_in()
            self._flush_merged_audio()
        elif action == "end_turn":
            self._end_turn(group)
        # "config" and unknown actions are accepted but currently no-ops.

    def _end_turn(self, group: asyncio.TaskGroup) -> None:
        """Close the current audio stream and start a turn if none is running."""
        self._audio.end_turn()
        if self._turn_task is None or self._turn_task.done():
            self._turn_task = group.create_task(self._run_turn(), name="ws-turn")

    async def _run_turn(self) -> None:
        """Run one pipeline turn over the buffered audio, then reset the stream."""
        try:
            await self._pipeline.run_turn(self._audio.frames())
        except Exception:  # noqa: BLE001 - a failed turn must not kill the socket
            logger.exception("pipeline turn failed")
        finally:
            self._audio.reset()

    async def _audio_pump(self) -> None:
        """Move PCM frames off the pipeline's outbound queue to the merged queue."""
        while True:
            frame = await self._pipeline.outbound.get()
            self._pipeline.outbound.task_done()
            await self._merged.put(frame)

    def _flush_merged_audio(self) -> int:
        """Drop queued audio frames from the merged queue, keeping control events.

        Called right after `barge_in()` (which has cancelled the producer and
        drained the pipeline's own queue): some PCM frames may already have been
        pumped onto `_merged` ahead of the ``interrupted`` event. Synchronous and
        await-free, so it runs atomically w.r.t. the writer/pump — bytes are
        dropped, control dicts (incl. ``interrupted``) are re-queued in order, so
        the interrupt reaches the client before any residual speech.
        """
        kept: list[Any] = []
        dropped = 0
        while True:
            try:
                item = self._merged.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._merged.task_done()
            if isinstance(item, (bytes, bytearray)):
                dropped += 1
            else:
                kept.append(item)
        for item in kept:
            self._merged.put_nowait(item)
        if dropped:
            logger.debug("barge-in dropped %d pumped audio frames", dropped)
        return dropped

    async def _writer(self) -> None:
        """SOLE sender: drain the merged queue, sending JSON or binary frames."""
        while True:
            item = await self._merged.get()
            if item is _STOP:
                return
            try:
                await self._send(item)
            except Exception:  # noqa: BLE001 - a send failure means the peer is gone
                logger.info("websocket send failed; ending writer")
                return

    async def _send(self, item: Any) -> None:
        """Send one item: dict -> JSON event, bytes -> binary audio frame."""
        if isinstance(item, (bytes, bytearray)):
            await self._ws.send_bytes(bytes(item))
        else:
            await self._ws.send_json(item)

    async def _pipeline_aclose(self) -> None:
        """Best-effort cleanup of any in-flight turn on disconnect."""
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()


class _Disconnect(Exception):
    """Internal signal that the peer closed the WebSocket."""


def _parse_control(raw: str) -> str:
    """Extract the control action from a JSON text frame; ``""`` if unparseable."""
    import json

    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.debug("ignoring non-JSON control frame: %r", raw)
        return ""
    return str(obj.get("type", "")) if isinstance(obj, dict) else ""


async def websocket_endpoint(ws: WebSocket, make_pipeline: PipelineFactory) -> None:
    """Handle one ``/ws`` connection end to end.

    ``make_pipeline`` builds a `Pipeline` from this connection's emit callback,
    binding it to the shared, already-loaded providers held on app state.
    """
    await _Connection(ws, make_pipeline).run()
