"""Proactive-push (``say``) box routing — the morning-brief overlay fix.

A proactive push (the scheduled morning brief) arrives on its OWN short-lived
connection (``scripts/morning_brief.py``), so the glow that draws the box is a
DIFFERENT client. The v1 bug: ``_speak_text`` sent the box events back to the
poking connection only, so the glow never saw them and the box stayed empty.
These tests pin the fix — display events BROADCAST to every client and carry a
``summon`` flag so the glow reveals its (normally F5-gated) box.

Same cheap harness as ``test_voice_loop_notes``: ``object.__new__(VoiceLoop)``
so no real STT/TTS/audio/harness is constructed.
"""

from __future__ import annotations

import pytest

from voice_loop import VoiceLoop


class FakeWS:
    """Records every JSON message sent to it (decoded back to a dict)."""

    def __init__(self, *, broken: bool = False) -> None:
        self.sent: list[dict] = []
        self.broken = broken

    async def send(self, payload: str) -> None:
        if self.broken:
            raise ConnectionError("socket gone")
        import json

        self.sent.append(json.loads(payload))


class _NoopGate:
    def reset(self) -> None:  # pragma: no cover - trivial
        pass


class _NoopPlayer:
    def set_gain(self, _g: float) -> None:  # pragma: no cover - trivial
        pass


def _say_loop(*clients: FakeWS) -> VoiceLoop:
    """A VoiceLoop wired with only what ``_speak_text``/``_broadcast`` touch."""
    vl = object.__new__(VoiceLoop)
    vl.clients = set(clients)
    vl.gate = _NoopGate()
    vl.player = _NoopPlayer()
    vl.listening = False
    vl.speaking = False

    async def _noop_clause(_text: str) -> None:
        return None

    async def _noop_drain() -> None:
        return None

    vl._speak_clause = _noop_clause   # don't touch real TTS
    vl._drain_playback = _noop_drain  # don't wait on a real speaker buffer
    return vl


async def test_say_summons_the_box_on_a_DIFFERENT_client() -> None:
    # glow = the persistent overlay; poker = the morning-brief connection that
    # sent the push. The brief text must reach the glow, not just the poker.
    glow, poker = FakeWS(), FakeWS()
    vl = _say_loop(glow, poker)

    await vl._speak_text(poker, "Good morning. Two events today.")

    for ws in (glow, poker):
        summons = [m for m in ws.sent if m.get("summon")]
        assert summons, "every client should receive the summon"
        assert summons[0]["text"] == "Good morning. Two events today."


async def test_say_brackets_the_push_with_turn_start_and_end() -> None:
    glow = FakeWS()
    vl = _say_loop(glow)

    await vl._speak_text(glow, "hello")

    kinds = [m.get("turn") for m in glow.sent if "turn" in m]
    assert kinds == ["start", "end"]
    # morning_brief.py disconnects on turn:end, so it must be broadcast last-ish.
    assert glow.sent[-1].get("state") in {"idle", "listening"}


async def test_a_dead_socket_does_not_block_delivery_to_others() -> None:
    dead, live = FakeWS(broken=True), FakeWS()
    vl = _say_loop(dead, live)

    await vl._speak_text(dead, "brief")

    # the live glow still got the whole sequence despite the dead poker
    assert any(m.get("summon") for m in live.sent)
    assert [m.get("turn") for m in live.sent if "turn" in m] == ["start", "end"]
