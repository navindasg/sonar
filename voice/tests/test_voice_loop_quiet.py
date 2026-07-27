"""The assistant stays QUIET unless you're interacting with it.

Navin's rule (2026-07-26): the harness must not listen to, talk to, or act on
him when he isn't interacting with it, and F5 must always cut it off. Notes is
deliberately exempt from the F5 rule — it's a separate appliance with its own
lifecycle — but that exemption is about the MIC, not about Sonar's voice.

The violation these pin: ``_on_notes_done`` spoke "Done taking notes…" out loud
through Kokoro whenever a session ended. It fires with the overlay closed (the
branch above it literally tests ``not self.listening``), long after the user last
interacted, and ``notes/controller.py`` runs ``wants_notes_stop()`` against every
transcribed utterance — so a *meeting participant* saying a stop phrase made
Sonar talk into the call. Same shape as the 2026-07-24 morning-brief incident.

The notes window already flips to its review screen off the session's own state
broadcast, so the speech was redundant as well as dangerous.
"""

from __future__ import annotations

from voice_loop import VoiceLoop
from notes import session as notes_session


class _Recorder:
    """Stands in for the bits _on_notes_done touches, recording what it did."""

    def __init__(self) -> None:
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1

    def reset_states(self) -> None:  # silero's spelling
        self.reset_calls += 1


class _NotesState:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeNotes:
    def __init__(self, status: str) -> None:
        self.state = _NotesState(status)


def _done_loop(*, status: str, listening: bool) -> tuple[VoiceLoop, list[str]]:
    """A VoiceLoop with only what ``_on_notes_done`` reads. Returns the loop plus
    a list that captures anything it tried to SAY."""
    said: list[str] = []
    vl = object.__new__(VoiceLoop)
    vl.notes = _FakeNotes(status)
    vl.listening = listening
    vl.endpointer = _Recorder()
    vl.silero = _Recorder()
    vl._notes_ws = object()
    vl._response_task = None
    vl.stopped_mic = False

    def _stop_mic() -> None:
        vl.stopped_mic = True

    def _start_say(_ws, text: str) -> None:
        said.append(text)

    vl.stop_mic = _stop_mic
    vl._start_say = _start_say
    return vl, said


async def test_ending_notes_never_speaks_with_the_overlay_closed() -> None:
    """THE regression guard: a third party's stop phrase must not make Sonar
    talk into a live meeting."""
    vl, said = _done_loop(status=notes_session.REVIEW, listening=False)

    await vl._on_notes_done()

    assert said == [], f"assistant spoke with the overlay closed: {said}"


async def test_ending_notes_never_speaks_even_mid_session() -> None:
    """Notes is exempt from F5, but Sonar's VOICE is not: ending a session is
    the notes window's news to deliver, never the speakers'."""
    vl, said = _done_loop(status=notes_session.REVIEW, listening=True)

    await vl._on_notes_done()

    assert said == []


async def test_ending_notes_hands_the_mic_back_when_not_listening() -> None:
    """Unchanged behaviour: no F5 session means nothing should hold the mic."""
    vl, _ = _done_loop(status=notes_session.REVIEW, listening=False)

    await vl._on_notes_done()

    assert vl.stopped_mic is True


async def test_ending_notes_keeps_the_mic_for_a_live_f5_session() -> None:
    vl, _ = _done_loop(status=notes_session.REVIEW, listening=True)

    await vl._on_notes_done()

    assert vl.stopped_mic is False


async def test_discarded_session_is_also_silent() -> None:
    """A discarded session never reaches REVIEW; it must be silent too."""
    vl, said = _done_loop(status=notes_session.RECORDING, listening=False)

    await vl._on_notes_done()

    assert said == []
