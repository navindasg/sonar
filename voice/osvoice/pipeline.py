"""Per-connection conversation orchestration (asyncio).

A `Pipeline` owns one conversation: the three providers (STT/LLM/TTS), an
immutable, length-bounded message history, and an outbound PCM queue the
transport drains. `run_turn` wires the stages — STT.stream ->
aggregate(LLM.stream) -> TTS.stream -> outbound queue — and surfaces progress
through an injected async event callback so the transport layer (and only it)
decides how to put bytes on the wire.

Transport concerns stay out of this module: events are plain `PipelineEvent`
values, audio leaves via the queue, and barge-in is a method the reader calls.
Barge-in must be fast (<60 ms): it cancels the in-flight LLM+TTS task,
drains-and-drops the queue, and emits an ``interrupted`` event.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Final

from osvoice.aggregator import aggregate
from osvoice.contracts import LLMProvider, STTProvider, TTSProvider, Transcript
from osvoice.metrics import (
    FIRST_AUDIO_OUT,
    LLM_FIRST_TOKEN,
    STT_FINAL,
    TTS_FIRST_FRAME,
    TURN_START,
    TurnTimer,
)

logger = logging.getLogger("osvoice.pipeline")

# Outbound audio queue depth: bounded so a slow consumer applies backpressure
# rather than letting TTS run unboundedly ahead of playback.
_OUTBOUND_MAXSIZE: Final = 256

# Keep history short — voice turns are latency-sensitive and the model context is
# small. We retain the system prompt plus the most recent N user/assistant turns.
_DEFAULT_HISTORY_TURNS: Final = 8

_DEFAULT_SYSTEM_PROMPT: Final = (
    "You are a helpful, concise voice assistant. Reply in short, natural "
    "spoken sentences."
)


# Event kinds the pipeline emits; the transport maps these to wire messages.
PARTIAL: Final = "partial"
FINAL: Final = "final"
ASSISTANT_TEXT: Final = "assistant_text"
SPEAKING_START: Final = "speaking_start"
SPEAKING_END: Final = "speaking_end"
INTERRUPTED: Final = "interrupted"


@dataclass(frozen=True)
class PipelineEvent:
    """An immutable progress signal emitted during a turn.

    ``text`` carries the payload for text-bearing events (partial/final
    transcripts, assistant text); control events leave it empty.
    """

    kind: str
    text: str = ""


EventCallback = Callable[[PipelineEvent], Awaitable[None]]


class Pipeline:
    """One conversation turn-runner bound to a single connection.

    Construct per WebSocket connection with the shared, already-loaded providers
    and an async ``emit`` callback. Audio out is pushed onto `outbound`; the
    transport's single writer task drains it.
    """

    def __init__(
        self,
        stt: STTProvider,
        lm: LLMProvider,
        tts: TTSProvider,
        emit: EventCallback,
        system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
        history_turns: int = _DEFAULT_HISTORY_TURNS,
        metrics: bool = False,
    ) -> None:
        self._stt = stt
        self._lm = lm
        self._tts = tts
        self._emit = emit
        self._system = {"role": "system", "content": system_prompt}
        self._history_turns = history_turns
        self._metrics = metrics
        self._history: tuple[dict[str, str], ...] = ()
        self.outbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_OUTBOUND_MAXSIZE)
        self._response_task: asyncio.Task[None] | None = None

    async def run_turn(self, audio_in: AsyncIterator[bytes]) -> None:
        """Run one full turn: transcribe ``audio_in``, then answer in voice.

        Emits partial transcripts as STT revises, a final transcript on
        endpointing, then (in a cancellable task) the assistant text and speech.
        Returns once the response task finishes or is cancelled by barge-in.
        """
        timer = TurnTimer() if self._metrics else None
        if timer is not None:
            timer.mark(TURN_START)

        final = await self._transcribe(audio_in, timer)
        if final is None or not final.text.strip():
            return

        self._history = _append(
            self._history, {"role": "user", "content": final.text.strip()},
            self._history_turns,
        )
        await self._respond(timer)

    async def _transcribe(
        self, audio_in: AsyncIterator[bytes], timer: TurnTimer | None
    ) -> Transcript | None:
        """Drive STT, emitting partials; return the final Transcript (or None)."""
        final: Transcript | None = None
        async for transcript in self._stt.stream(audio_in):
            if transcript.is_final:
                final = transcript
                if timer is not None:
                    timer.mark(STT_FINAL)
                await self._emit(PipelineEvent(FINAL, transcript.text))
            elif transcript.text:
                await self._emit(PipelineEvent(PARTIAL, transcript.text))
        return final

    async def _respond(self, timer: TurnTimer | None) -> None:
        """Generate and speak the assistant reply in a barge-in-cancellable task."""
        self._response_task = asyncio.create_task(self._generate_and_speak(timer))
        try:
            await self._response_task
        except asyncio.CancelledError:
            logger.debug("response task cancelled (barge-in)")
        finally:
            self._response_task = None
            if timer is not None:
                timer.log_summary()

    async def _generate_and_speak(self, timer: TurnTimer | None) -> None:
        """LLM -> clause aggregation -> TTS -> outbound queue, with text capture."""
        spoken: list[str] = []
        clauses = aggregate(self._collect_tokens(spoken, timer))
        await self._emit(PipelineEvent(SPEAKING_START))
        first_frame = True
        try:
            async for frame in self._tts.stream(clauses):
                if not frame:
                    continue
                if first_frame:
                    first_frame = False
                    if timer is not None:
                        timer.mark(TTS_FIRST_FRAME)
                        timer.mark(FIRST_AUDIO_OUT)
                await self.outbound.put(frame)
        finally:
            await self._finish_response(spoken)

    async def _collect_tokens(
        self, spoken: list[str], timer: TurnTimer | None
    ) -> AsyncIterator[str]:
        """Stream LLM token deltas, recording the first-token mark and full text."""
        first = True
        async for delta in self._lm.stream(self._messages()):
            if first:
                first = False
                if timer is not None:
                    timer.mark(LLM_FIRST_TOKEN)
            spoken.append(delta)
            yield delta

    async def _finish_response(self, spoken: list[str]) -> None:
        """Append the assistant turn to history and emit the closing events."""
        text = "".join(spoken).strip()
        if text:
            self._history = _append(
                self._history, {"role": "assistant", "content": text},
                self._history_turns,
            )
            await self._emit(PipelineEvent(ASSISTANT_TEXT, text))
        await self._emit(PipelineEvent(SPEAKING_END))

    def _messages(self) -> list[dict[str, str]]:
        """Build the OpenAI-style messages list: system prompt + bounded history.

        Hands providers fresh copies so a backend that mutates a message dict can
        never corrupt the stored system prompt or conversation history.
        """
        return [dict(self._system), *(dict(m) for m in self._history)]

    async def barge_in(self) -> None:
        """Interrupt the in-flight response: cancel, flush audio, emit interrupted.

        Designed for <60 ms: cancelling the task is immediate and draining the
        bounded queue is O(queue depth). The cancelled task's `finally` still
        appends partial assistant text to history.
        """
        task = self._response_task
        if task is not None and not task.done():
            task.cancel()
        flushed = self._flush_outbound()
        if flushed:
            logger.debug("barge-in flushed %d queued audio frames", flushed)
        await self._emit(PipelineEvent(INTERRUPTED))

    def _flush_outbound(self) -> int:
        """Drain-and-drop every queued audio frame; return how many were dropped."""
        dropped = 0
        while True:
            try:
                self.outbound.get_nowait()
            except asyncio.QueueEmpty:
                return dropped
            self.outbound.task_done()
            dropped += 1


def _append(
    history: tuple[dict[str, str], ...], message: dict[str, str], max_turns: int
) -> tuple[dict[str, str], ...]:
    """Return a new history with ``message`` appended, truncated to recent turns.

    Immutable: never mutates ``history``. ``max_turns`` counts user+assistant
    messages, so the cap is ``2 * max_turns`` retained entries.
    """
    grown = (*history, dict(message))  # copy so stored history never aliases callers
    limit = max_turns * 2
    return grown[-limit:] if len(grown) > limit else grown
