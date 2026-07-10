"""Unit tests for the barge-in decision logic (echo_gate.EchoGate)."""
from __future__ import annotations

import pytest

from echo_gate import EchoGate, GateDecision


def _gate() -> EchoGate:
    # Explicit params so the test does not ride on the module defaults.
    return EchoGate(floor=0.06, margin=2.2, vad_on=0.6, duck_after=2, trigger_frames=5)


def test_rejects_bad_thresholds() -> None:
    with pytest.raises(ValueError):
        EchoGate(trigger_frames=0)
    with pytest.raises(ValueError):
        EchoGate(duck_after=0)
    with pytest.raises(ValueError):
        EchoGate(duck_after=6, trigger_frames=5)  # duck_after > trigger_frames


def test_silence_never_barges() -> None:
    gate = _gate()
    for _ in range(20):
        d = gate.observe(vad_prob=0.0, mic_rms=0.0, tts_rms=0.0)
        assert d == GateDecision(duck=False, barge_in=False)


def test_echo_below_relative_threshold_does_not_trip() -> None:
    """Voiced-looking echo that stays under margin*tts_rms is not a barge-in."""
    gate = _gate()
    for _ in range(20):
        # threshold = max(0.06, 2.2*0.1) = 0.22; echo at 0.15 < 0.22 -> not suspect
        d = gate.observe(vad_prob=0.9, mic_rms=0.15, tts_rms=0.1)
        assert not d.barge_in


def test_ducks_then_barges_when_user_talks_over() -> None:
    gate = _gate()
    # Loud, voiced, well above threshold=max(0.06, 2.2*0.1)=0.22.
    decisions = [gate.observe(0.9, 0.5, 0.1) for _ in range(5)]
    assert not decisions[0].duck                      # 1 suspect frame
    assert decisions[1].duck and not decisions[1].barge_in  # duck_after=2
    assert not decisions[3].barge_in                  # 4 suspect frames
    assert decisions[4].barge_in                      # trigger_frames=5


def test_single_clean_frame_resets_the_run() -> None:
    gate = _gate()
    for _ in range(4):
        assert not gate.observe(0.9, 0.5, 0.1).barge_in
    gate.observe(0.0, 0.0, 0.1)                        # non-suspect -> reset
    # Would have barged on the 5th consecutive; now needs a fresh run of 5.
    for _ in range(4):
        assert not gate.observe(0.9, 0.5, 0.1).barge_in
    assert gate.observe(0.9, 0.5, 0.1).barge_in


def test_reset_clears_pending_run() -> None:
    gate = _gate()
    for _ in range(4):
        gate.observe(0.9, 0.5, 0.1)
    gate.reset()
    assert not gate.observe(0.9, 0.5, 0.1).barge_in   # counter back to 1, not 5


def test_floor_gates_barge_in_when_nothing_is_playing() -> None:
    """With tts_rms=0 the absolute floor still guards against quiet noise."""
    gate = _gate()
    # 0.05 < floor 0.06 -> never suspect even though voiced.
    for _ in range(10):
        assert not gate.observe(0.9, 0.05, 0.0).barge_in
    # 0.2 >= floor -> trips after trigger_frames.
    for _ in range(4):
        assert not gate.observe(0.9, 0.2, 0.0).barge_in
    assert gate.observe(0.9, 0.2, 0.0).barge_in
