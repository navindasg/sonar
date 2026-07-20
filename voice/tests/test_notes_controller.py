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
        self.windows: list[tuple[int, int, np.ndarray]] = []  # for the split path
        self.loaded = False

    async def load(self) -> None:
        self.loaded = True

    async def embed(self, _pcm: bytes) -> np.ndarray | None:
        return self.queue.pop(0) if self.queue else None

    async def embed_windows(self, _pcm: bytes):
        return list(self.windows)


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


async def test_long_utterance_splits_on_mid_utterance_speaker_change(rig) -> None:
    # A merged turn (no full pause) where the voice changes partway is split into
    # two segments with different speakers and re-transcribed per part.
    ctl, stt, emb = rig
    A = np.array([1.0, 0.0], dtype=np.float32)
    B = np.array([0.0, 1.0], dtype=np.float32)
    emb.windows = [
        (0, 24000, A), (8000, 32000, A), (16000, 40000, A),   # speaker A
        (24000, 48000, B), (32000, 56000, B), (40000, 64000, B),  # speaker B
    ]
    stt.queue = [
        "hey how are you doing today good thanks",   # whole-utterance (stop-check)
        "hey how are you doing today",               # run 1 (A)
        "good thanks",                               # run 2 (B)
    ]
    _speak(ctl, frames=120)                          # ~3.9 s -> past the 2.5 s split floor
    await _drain(ctl)

    segs = ctl.state.segments
    assert [s.text for s in segs] == ["hey how are you doing today", "good thanks"]
    assert segs[0].speaker != segs[1].speaker
    assert segs[0].t0 < segs[0].t1 <= segs[1].t0 < segs[1].t1


async def test_long_single_voice_utterance_stays_one_segment(rig) -> None:
    # Windows that don't change speaker must NOT split.
    ctl, stt, emb = rig
    A = np.array([1.0, 0.0], dtype=np.float32)
    emb.windows = [(0, 24000, A), (8000, 32000, A), (16000, 40000, A), (24000, 48000, A)]
    stt.queue = ["one long uninterrupted thought from a single speaker"]
    _speak(ctl, frames=120)
    await _drain(ctl)
    assert [s.text for s in ctl.state.segments] == [
        "one long uninterrupted thought from a single speaker"
    ]


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


# --- regression: #1 restart, #10 discard/end race, #15 degradation ---------


class RaisingEmbedder:
    """An embedder whose backend never loads (no Metal / speechbrain)."""

    async def load(self) -> None:
        raise RuntimeError("embedder backend unavailable")

    async def embed(self, _pcm: bytes) -> np.ndarray | None:  # pragma: no cover
        return None


async def test_second_session_records_and_ends_without_deadlock(tmp_path: Path) -> None:
    # #1 regression: the controller is long-lived and reused for every "take
    # notes". A worker parked on the FIRST session's queue used to leak, so
    # session 2's feed() enqueued to a queue nothing consumed and end()'s
    # queue.join() hung forever. A fresh worker must consume the new queue.
    stt, emb = FakeStt(), FakeEmbedder()
    ctl = NotesController(
        transcribe=stt, vault_path=tmp_path, embedder=emb,
        open_browser=False, port=0,
    )
    ctl._summarize = _fake_summarize
    try:
        # session 1
        await ctl.start(now=datetime(2026, 7, 15, 9, 0))
        ctl.begin_capture()
        stt.queue = ["first session line"]
        emb.queue = [np.array([1.0, 0.0])]
        _speak(ctl)
        await _drain(ctl)
        await ctl.end()
        assert ctl.state.status == sess.REVIEW

        # session 2 on the SAME controller
        await ctl.start(now=datetime(2026, 7, 15, 10, 0))
        ctl.begin_capture()
        stt.queue = ["second session line"]
        emb.queue = [np.array([1.0, 0.0])]
        _speak(ctl)
        await _drain(ctl)
        assert [s.text for s in ctl.state.segments] == ["second session line"]

        # end() must not deadlock on the drained queue.join()
        await asyncio.wait_for(ctl.end(), timeout=3.0)
        assert ctl.state.status == sess.REVIEW
    finally:
        await ctl.aclose()


async def test_discard_during_summary_wins_and_fires_on_ended_once(tmp_path: Path) -> None:
    # #10 regression: a Discard landing while end() awaits the (slow) AI overview
    # must win — the terminal DISCARDED state is never clobbered back to REVIEW —
    # and on_ended must fire exactly once across the interleaved end()/discard().
    fired = {"n": 0}

    async def on_ended() -> None:
        fired["n"] += 1

    stt, emb = FakeStt(), FakeEmbedder()
    ctl = NotesController(
        transcribe=stt, vault_path=tmp_path, embedder=emb,
        open_browser=False, port=0, on_ended=on_ended,
    )
    gate = asyncio.Event()

    async def slow_summarize() -> str:
        await gate.wait()        # park end() inside the summary await
        return "### Summary"

    ctl._summarize = slow_summarize
    try:
        await ctl.start(now=datetime(2026, 7, 15, 9, 0))
        ctl.begin_capture()
        stt.queue = ["a real line"]
        emb.queue = [np.array([1.0, 0.0])]
        _speak(ctl)
        await _drain(ctl)

        end_task = asyncio.create_task(ctl.end())
        await _until(lambda: ctl.state.status == sess.SUMMARIZING)

        await ctl.discard()                    # discard lands mid-summary
        assert ctl.state.status == sess.DISCARDED

        gate.set()                             # let the slow summary return
        await asyncio.wait_for(end_task, timeout=3.0)

        assert ctl.state.status == sess.DISCARDED   # end() did not clobber it
        assert fired["n"] == 1                       # on_ended fired exactly once
    finally:
        await ctl.aclose()


async def test_embedder_load_failure_sets_diarization_degraded(tmp_path: Path) -> None:
    # #15 regression: when the speaker embedder can't load, the session degrades
    # to a single speaker — surface that through the SHARED CONTRACT flag instead
    # of failing silently, so the UI can warn the user.
    ctl = NotesController(
        transcribe=FakeStt(), vault_path=tmp_path, embedder=RaisingEmbedder(),
        open_browser=False, port=0,
    )
    ctl._summarize = _fake_summarize
    try:
        await ctl.start(now=datetime(2026, 7, 15, 9, 0))
        assert ctl.state_json()["diarization_degraded"] is True
    finally:
        await ctl.aclose()
