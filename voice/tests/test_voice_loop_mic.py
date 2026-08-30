"""The mic belongs to the PROCESS, not to whichever socket happens to be open.

Two defects lived here, and they compound. (1) ``handler`` spawned a
``_consume`` task per connection while there is exactly one mic queue and one
stateful endpointer — asyncio.Queue hands each frame to exactly ONE getter, so
with the overlay and a morning-brief poker both connected the audio was split
frame-by-frame: SPEECH_START landed in one task, TURN_END in the other (whose
utterance was empty), and the spoken turn vanished. (2) ``handler``'s finally
cleared ``listening``, closed the mic and cancelled the in-flight turn for the
WHOLE process, so that same poker disconnecting killed the user's live session.

Both are driven here through the real ``handler`` with fake sockets — no MLX,
torch, audio or harness (VoiceLoop is built with ``object.__new__``).
"""

from __future__ import annotations

import json

from voice_loop import VoiceLoop


class FakeWS:
    """A connection: yields its queued client messages, then closes."""

    def __init__(self, *msgs: str) -> None:
        self._msgs = list(msgs)
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def __aiter__(self):
        for msg in self._msgs:
            yield msg


class _FakeNotes:
    def __init__(self, *, recording: bool = False, active: bool = False) -> None:
        self.recording = recording
        self.active = active


def _handler_loop(*, notes_active: bool = False) -> VoiceLoop:
    """A VoiceLoop with only what ``handler`` and its teardown touch."""
    vl = object.__new__(VoiceLoop)
    vl.clients = set()
    vl.notes = _FakeNotes(recording=notes_active, active=notes_active)
    vl.listening = True
    vl.history = []
    vl._response_task = None
    vl._consumer = None
    vl.stopped_mic = False
    vl.silenced = False

    def _stop_mic() -> None:
        vl.stopped_mic = True

    async def _silence() -> None:
        vl.silenced = True

    vl.stop_mic = _stop_mic
    vl._silence = _silence
    return vl


async def test_handler_does_not_spawn_a_mic_consumer_per_connection() -> None:
    """THE split-mic guard: the drain is started once in load(), never here."""
    vl = _handler_loop()
    spawned: list[object] = []
    vl._consume = lambda *a, **kw: spawned.append(a)   # would be create_task'd

    await vl.handler(FakeWS())
    await vl.handler(FakeWS())

    assert spawned == [], "handler must not start its own mic consumer"


async def test_a_secondary_client_leaving_keeps_the_live_session() -> None:
    """`sonar.sh brief` opens its own socket to :8770, speaks, and disconnects.
    That must not close the mic the overlay is still using, nor cancel the turn
    the overlay owns."""
    vl = _handler_loop()
    glow = FakeWS()
    vl.clients.add(glow)          # the overlay, still connected

    await vl.handler(FakeWS())    # the poker connects and immediately leaves

    assert vl.clients == {glow}
    assert vl.listening is True
    assert vl.stopped_mic is False
    assert vl.silenced is False


async def test_the_last_client_leaving_tears_the_session_down() -> None:
    vl = _handler_loop()

    await vl.handler(FakeWS())

    assert vl.clients == set()
    assert vl.listening is False
    assert vl.stopped_mic is True
    assert vl.silenced is True


async def test_the_last_client_leaving_mid_notes_keeps_the_mic() -> None:
    """The notes page is a separate window with its own lifecycle: closing the
    overlay must not stop capturing a meeting. Safe now that the mic loop
    outlives the connection — nothing is left draining an orphaned queue."""
    vl = _handler_loop(notes_active=True)

    await vl.handler(FakeWS())

    assert vl.stopped_mic is False
    assert vl.listening is False


async def test_transcripts_reach_every_client_not_just_one_socket() -> None:
    """The mic loop owns no connection, so what it emits has to broadcast."""
    vl = _handler_loop()
    glow, poker = FakeWS(), FakeWS()
    vl.clients = {glow, poker}

    async def _transcribe(_pcm: bytes) -> str:
        return "good morning"

    vl._transcribe_pcm = _transcribe

    text = await vl._emit_transcript([b"\x00" * 8000], final=True)

    assert text == "good morning"
    for ws in (glow, poker):
        assert {"transcript": "good morning", "partial": False} in ws.sent
