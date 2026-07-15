"""Controller pipeline: frames -> endpointed utterance -> diarized segment ->
end/summarize/save, with fake STT + embedder (no MLX, torch, or Ollama)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from notes import session as sess
from notes.controller import NotesController

FRAME = b"\x00" * 1024        # one 512-sample PCM16 frame (32 ms)
SILENCE_FRAMES = 24           # > 700 ms / 32 ms -> guarantees TURN_END


class FakeEmbedder:
    """Returns whatever the test queues next; None when exhausted."""

    def __init__(self) -> None:
        self.queue: list[np.ndarray | None] = []
        self.loaded = False

    async def load(self) -> None:
        self.loaded = True

    async def embed(self, _pcm: bytes) -> np.ndarray | None:
        return self.queue.pop(0) if self.queue else None


class FakeStt:
    def __init__(self) -> None:
        self.queue: list[str] = []

    async def __call__(self, _pcm: bytes) -> str:
        return self.queue.pop(0) if self.queue else ""


@pytest.fixture
async def rig(tmp_path: Path):
    stt, emb = FakeStt(), FakeEmbedder()
    ctl = NotesController(
        transcribe=stt, vault_path=tmp_path, embedder=emb,
        open_browser=False, port=0,
    )
    ctl._summarize = _fake_summarize  # no Ollama in unit tests
    await ctl.start(now=datetime(2026, 7, 15, 9, 0))
    ctl.begin_capture()
    try:
        yield ctl, stt, emb
    finally:
        await ctl.aclose()


async def _fake_summarize() -> str:
    return "### Summary\n\n- fake overview"


def _speak(ctl: NotesController, frames: int = 40) -> None:
    """Push one spoken utterance through feed() — 40 speech frames is ~1.3 s,
    comfortably past diarize's 1.0 s new-speaker floor."""
    for _ in range(frames):
        ctl.feed(FRAME, 0.9)
    for _ in range(SILENCE_FRAMES):
        ctl.feed(FRAME, 0.0)


async def _drain(ctl: NotesController) -> None:
    await ctl._queue.join()
    await asyncio.sleep(0)        # let broadcast tasks settle


async def _until(cond, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


async def test_utterances_become_diarized_segments(rig) -> None:
    ctl, stt, emb = rig
    stt.queue = ["good morning", "hi there"]
    emb.queue = [np.array([1.0, 0.0]), np.array([0.0, 1.0])]

    _speak(ctl)
    _speak(ctl)
    await _drain(ctl)

    segs = ctl.state.segments
    assert [s.text for s in segs] == ["good morning", "hi there"]
    assert [s.speaker for s in segs] == ["S1", "S2"]
    assert segs[0].t0 < segs[0].t1 <= segs[1].t0 < segs[1].t1
    assert ctl.state.status == sess.RECORDING


async def test_empty_transcription_adds_nothing(rig) -> None:
    ctl, stt, _emb = rig
    stt.queue = ["   "]
    _speak(ctl)
    await _drain(ctl)
    assert ctl.state.segments == ()


async def test_frames_after_end_are_ignored(rig) -> None:
    ctl, stt, emb = rig
    stt.queue = ["only line", "should never land"]
    emb.queue = [np.array([1.0, 0.0])]
    _speak(ctl)
    await _drain(ctl)
    await ctl.end()

    _speak(ctl)                              # mic tap is closed now
    await _drain(ctl)
    assert [s.text for s in ctl.state.segments] == ["only line"]


async def test_spoken_stop_phrase_ends_the_session(rig) -> None:
    ctl, stt, emb = rig
    stt.queue = ["let's begin", "stop taking notes"]
    emb.queue = [np.array([1.0, 0.0])]

    _speak(ctl)
    _speak(ctl)
    await _until(lambda: ctl.state.status == sess.REVIEW)

    assert not ctl.wants_frames
    assert [s.text for s in ctl.state.segments] == ["let's begin"]  # command excluded
    assert ctl.state.summary_md == "### Summary\n\n- fake overview"


async def test_end_save_and_resave(rig, tmp_path: Path) -> None:
    ctl, stt, emb = rig
    stt.queue = ["we decided things"]
    emb.queue = [np.array([1.0, 0.0])]
    _speak(ctl)
    await _drain(ctl)

    await ctl.end()
    assert ctl.state.status == sess.REVIEW

    await ctl.save()
    assert ctl.state.status == sess.SAVED
    saved = tmp_path / ctl.state.saved_path
    assert saved.is_file()
    body = saved.read_text(encoding="utf-8")
    assert "we decided things" in body and "- fake overview" in body

    await ctl.apply_client_op({"op": "edit_summary", "markdown": "### EDITED"})
    await ctl.save()
    assert "### EDITED" in saved.read_text(encoding="utf-8")
    assert ctl.state.saved_path == str(saved.relative_to(tmp_path))


async def test_client_ops_edit_the_session(rig) -> None:
    ctl, stt, emb = rig
    stt.queue = ["hello world"]
    emb.queue = [np.array([1.0, 0.0])]
    _speak(ctl)
    await _drain(ctl)

    await ctl.apply_client_op({"op": "rename", "speaker": "S1", "name": "Navin"})
    await ctl.apply_client_op({"op": "edit_segment", "id": 0, "text": "hello, world"})
    await ctl.apply_client_op({"op": "set_title", "title": "Kickoff"})
    await ctl.apply_client_op({"op": "add_speaker"})
    await ctl.apply_client_op({"op": "reassign", "id": 0, "speaker": "S2"})
    await ctl.apply_client_op({"op": "unknown"})       # ignored

    s = ctl.state
    assert sess.display_name(s, "S1") == "Navin"
    assert s.title == "Kickoff"
    assert s.segments[0].text == "hello, world"
    assert s.segments[0].speaker == "S2"


async def test_discard_stops_feeding(rig) -> None:
    ctl, _stt, _emb = rig
    assert ctl.wants_frames
    await ctl.discard()
    assert not ctl.wants_frames
    assert ctl.state.status == sess.DISCARDED


async def test_feed_before_begin_capture_is_ignored(tmp_path: Path) -> None:
    ctl = NotesController(
        transcribe=FakeStt(), vault_path=tmp_path, embedder=FakeEmbedder(),
        open_browser=False, port=0,
    )
    await ctl.start(now=datetime(2026, 7, 15, 9, 0))
    try:
        _speak(ctl)                    # ack still playing: tap not open yet
        await _drain(ctl)
        assert ctl.state.segments == ()
        assert not ctl.wants_frames
    finally:
        await ctl.aclose()
