"""Voice-loop notes routing + mic-ownership decisions (#18).

These exercise the pure routing logic with a FAKE NotesController — no MLX,
torch, audio, or Ollama. `voice_loop` imports cleanly without any heavy backend
(the provider adapters keep those behind lazy imports), so we can drive the
routing methods directly. VoiceLoop is instantiated via ``object.__new__`` so
the real STT/TTS/audio constructors never run.
"""

from __future__ import annotations

import asyncio

import pytest

import voice_loop
from voice_loop import VoiceLoop


class FakeNotesController:
    """Minimal stand-in: just the surface voice_loop's routing touches."""

    def __init__(self, *, recording: bool = False, active: bool = False) -> None:
        self.recording = recording
        self.active = active
        self.title_hint: str | None = None
        self.capturing = False
        self.discarded = False
        self.state = None

    async def start(self, title_hint: str | None = None) -> str:
        self.title_hint = title_hint
        self.recording = True
        self.active = True
        return "http://127.0.0.1:0"

    def begin_capture(self) -> None:
        self.capturing = True

    async def discard(self) -> None:
        self.discarded = True


def _bare_loop() -> VoiceLoop:
    """A VoiceLoop with only the attributes the routing methods read — no real
    STT/TTS/audio/harness are constructed."""
    vl = object.__new__(VoiceLoop)
    vl.notes = FakeNotesController()
    vl._response_task = None
    vl._notes_ws = None
    return vl


def test_notes_guards_reflect_controller_state() -> None:
    vl = _bare_loop()

    # idle session: neither recording nor active
    vl.notes = FakeNotesController(recording=False, active=False)
    assert vl._notes_recording() is False
    assert vl._notes_active() is False

    # recording: both true
    vl.notes = FakeNotesController(recording=True, active=True)
    assert vl._notes_recording() is True
    assert vl._notes_active() is True

    # summarize gap: NOT recording, but still active (mic must stay out of the
    # assistant path until on_ended hands it back)
    vl.notes = FakeNotesController(recording=False, active=True)
    assert vl._notes_recording() is False
    assert vl._notes_active() is True

    # no controller at all
    vl.notes = None
    assert vl._notes_recording() is False
    assert vl._notes_active() is False


async def test_maybe_start_notes_routes_a_take_notes_command() -> None:
    vl = _bare_loop()
    calls: list[tuple] = []

    async def fake_speak(ws, text: str) -> None:
        calls.append(("speak", text))

    vl._speak_text = fake_speak
    vl.start_mic = lambda: calls.append(("mic",))

    routed = vl._maybe_start_notes("WS", "take notes on the budget review")
    assert routed is True                    # claimed the utterance
    await asyncio.wait_for(vl._response_task, timeout=2.0)

    # the session was actually started + capture opened AFTER the spoken ack
    assert vl.notes.recording is True
    assert vl.notes.title_hint == "the budget review"
    assert vl.notes.capturing is True
    assert ("mic",) in calls
    assert any(kind == "speak" for kind, *_ in calls)


async def test_maybe_start_notes_ignores_non_commands() -> None:
    vl = _bare_loop()
    # a plain question is NOT a notes command -> falls through to the harness path
    assert vl._maybe_start_notes("WS", "what's on my calendar tomorrow") is False
    assert vl._response_task is None


async def test_maybe_start_notes_no_op_while_already_recording() -> None:
    vl = _bare_loop()
    vl.notes = FakeNotesController(recording=True, active=True)
    # already inside a live meeting: a second "take notes" must not start another
    assert vl._maybe_start_notes("WS", "take notes") is False


def test_voice_loop_imports_without_heavy_backends() -> None:
    # #18 guard: the routing tests are only cheap because importing voice_loop
    # doesn't drag in MLX/torch/audio backends (they stay lazy in load()).
    import sys

    for backend in ("mlx", "torch", "parakeet_mlx", "silero_vad", "mlx_audio"):
        assert backend not in sys.modules, f"{backend} leaked at import time"
    assert hasattr(voice_loop, "VoiceLoop")
