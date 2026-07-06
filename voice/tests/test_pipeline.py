"""End-to-end Pipeline tests with tiny in-memory fake providers.

The fakes implement the STT/LLM/TTS protocols in pure asyncio (no models): STT
yields a couple of partials then a final, LLM yields token deltas, TTS yields one
PCM frame per clause. We assert (a) a full turn flows STT -> LLM -> aggregator ->
TTS onto the outbound queue and emits the expected events, and (b) barge_in()
cancels in-flight work and flushes the queue.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from osvoice.contracts import Transcript
from osvoice.pipeline import (
    ASSISTANT_TEXT,
    FINAL,
    INTERRUPTED,
    PARTIAL,
    SPEAKING_END,
    SPEAKING_START,
    Pipeline,
    PipelineEvent,
)


# --- fake providers ------------------------------------------------------------


class FakeSTT:
    """Yields two growing partials then one final Transcript."""

    def __init__(self, final_text: str = "hello there") -> None:
        self._final_text = final_text

    async def load(self) -> None:  # pragma: no cover - trivial
        return None

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        # Drain the input so the audio source is consumed like a real STT would.
        async for _ in audio:
            pass
        yield Transcript("hello", is_final=False)
        yield Transcript("hello there", is_final=False)
        yield Transcript(self._final_text, is_final=True)


class FakeLLM:
    """Yields fixed token deltas that aggregate into one spoken sentence."""

    def __init__(self, deltas: list[str] | None = None) -> None:
        self._deltas = deltas or ["Hi", " back", " to", " you", " friend", ". "]
        self.seen_messages: list[dict[str, str]] | None = None

    async def load(self) -> None:  # pragma: no cover - trivial
        return None

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        self.seen_messages = messages
        for delta in self._deltas:
            yield delta


class FakeTTS:
    """Emits one PCM frame per incoming clause (clause text encoded as bytes)."""

    async def load(self) -> None:  # pragma: no cover - trivial
        return None

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        async for clause in text:
            yield clause.encode("utf-8")


class BlockingTTS:
    """TTS that signals when speaking starts then blocks forever (for barge-in).

    Emits one frame to prove audio reached the queue, sets ``started`` so the
    test knows the response task is genuinely in-flight, then awaits an event
    that never fires — so only cancellation can stop it.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self._never = asyncio.Event()

    async def load(self) -> None:  # pragma: no cover - trivial
        return None

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        async for clause in text:
            yield clause.encode("utf-8")
            self.started.set()
            await self._never.wait()  # block until cancelled


# --- helpers -------------------------------------------------------------------


async def _silent_audio() -> AsyncIterator[bytes]:
    """A trivial PCM16 input stream (content irrelevant to the fakes)."""
    yield b"\x00\x00" * 256


def _recording_emit(sink: list[PipelineEvent]):
    async def emit(event: PipelineEvent) -> None:
        sink.append(event)

    return emit


async def _drain_queue(pipeline: Pipeline) -> list[bytes]:
    frames: list[bytes] = []
    while not pipeline.outbound.empty():
        frames.append(pipeline.outbound.get_nowait())
    return frames


# --- full-turn flow ------------------------------------------------------------


async def test_full_turn_flows_stt_llm_aggregator_tts_to_queue() -> None:
    # Arrange
    events: list[PipelineEvent] = []
    llm = FakeLLM()
    pipeline = Pipeline(
        stt=FakeSTT(final_text="hello there"),
        lm=llm,
        tts=FakeTTS(),
        emit=_recording_emit(events),
    )

    # Act
    await pipeline.run_turn(_silent_audio())
    frames = await _drain_queue(pipeline)

    # Assert: TTS produced audio for the aggregated assistant clause.
    assert frames == [b"Hi back to you friend."]

    # Assert: the event sequence covered partial -> final -> speaking lifecycle.
    kinds = [e.kind for e in events]
    assert PARTIAL in kinds
    assert FINAL in kinds
    assert kinds.index(SPEAKING_START) < kinds.index(SPEAKING_END)

    # Assert: the final transcript text was emitted.
    final_event = next(e for e in events if e.kind == FINAL)
    assert final_event.text == "hello there"

    # Assert: the assistant text was captured and emitted.
    assistant_event = next(e for e in events if e.kind == ASSISTANT_TEXT)
    assert assistant_event.text == "Hi back to you friend."

    # Assert: the user's transcript reached the LLM as a message.
    assert llm.seen_messages is not None
    assert llm.seen_messages[0]["role"] == "system"
    assert llm.seen_messages[-1] == {"role": "user", "content": "hello there"}


async def test_empty_final_transcript_skips_response() -> None:
    # Arrange: STT returns only whitespace as the final -> no LLM/TTS work.
    events: list[PipelineEvent] = []
    llm = FakeLLM()
    pipeline = Pipeline(
        stt=FakeSTT(final_text="   "),
        lm=llm,
        tts=FakeTTS(),
        emit=_recording_emit(events),
    )

    # Act
    await pipeline.run_turn(_silent_audio())

    # Assert: no response stages ran.
    assert llm.seen_messages is None
    assert pipeline.outbound.empty()
    assert SPEAKING_START not in [e.kind for e in events]


# --- barge-in ------------------------------------------------------------------


async def test_barge_in_cancels_in_flight_work_and_flushes_queue() -> None:
    # Arrange: a TTS that blocks after emitting its first frame.
    events: list[PipelineEvent] = []
    tts = BlockingTTS()
    pipeline = Pipeline(
        stt=FakeSTT(final_text="please tell me a long story"),
        lm=FakeLLM(deltas=["A", " long", " story", " begins", " here", ". "]),
        tts=tts,
        emit=_recording_emit(events),
    )

    # Act: start the turn in the background and wait until TTS is speaking.
    turn = asyncio.create_task(pipeline.run_turn(_silent_audio()))
    await asyncio.wait_for(tts.started.wait(), timeout=1.0)
    assert not pipeline.outbound.empty()  # a frame is queued, work is in-flight

    # Act: barge in.
    await pipeline.barge_in()
    await asyncio.wait_for(turn, timeout=1.0)

    # Assert: the queue was flushed and an interrupted event was emitted.
    assert pipeline.outbound.empty()
    assert INTERRUPTED in [e.kind for e in events]

    # Assert: the in-flight response task was cleared (cancellation cleaned up).
    assert pipeline._response_task is None
