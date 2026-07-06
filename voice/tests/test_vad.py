"""Tests for the pure VAD endpointing state machines (no torch).

Endpointer and BargeInDetector carry no torch dependency, so we drive them with
synthetic per-frame speech probabilities and assert the hysteresis transitions:
SPEECH_START after the start frames, TURN_END after enough sub-threshold silence,
reset() clearing state, and barge-in requiring N consecutive speech frames.
"""
from __future__ import annotations

import pytest

from osvoice.vad import BargeInDetector, Endpointer, VadEvent


# --- Endpointer: start hysteresis ----------------------------------------------


def test_speech_start_requires_consecutive_frames() -> None:
    # Arrange: default needs 2 consecutive frames above start_threshold (0.6).
    ep = Endpointer()

    # Act / Assert: a single loud frame is not enough...
    assert ep.update(0.9) is None
    # ...the second consecutive loud frame triggers SPEECH_START.
    assert ep.update(0.9) is VadEvent.SPEECH_START


def test_non_consecutive_loud_frames_reset_start_counter() -> None:
    # Arrange
    ep = Endpointer(speech_frames_to_start=3)

    # Act: a quiet frame between loud ones must reset the run.
    assert ep.update(0.9) is None
    assert ep.update(0.1) is None  # resets the counter
    assert ep.update(0.9) is None
    assert ep.update(0.9) is None
    # Third *consecutive* loud frame finally starts speech.
    assert ep.update(0.9) is VadEvent.SPEECH_START


# --- Endpointer: turn end ------------------------------------------------------


def test_turn_end_after_silence_ms_of_subthreshold_frames() -> None:
    # Arrange: frame_ms=32, silence_ms=100 -> needs ceil(100/32)=4 silent frames.
    ep = Endpointer(silence_ms=100, frame_ms=32)
    assert ep.update(0.9) is None
    assert ep.update(0.9) is VadEvent.SPEECH_START

    # Act / Assert: sub-sustain frames (<=0.35) accumulate silence.
    assert ep.update(0.1) is None  # 32ms
    assert ep.update(0.1) is None  # 64ms
    assert ep.update(0.1) is None  # 96ms
    assert ep.update(0.1) is VadEvent.TURN_END  # 128ms >= 100ms


def test_sustained_speech_keeps_silence_accumulator_reset() -> None:
    # Arrange
    ep = Endpointer(silence_ms=100, frame_ms=32)
    ep.update(0.9)
    ep.update(0.9)  # SPEECH_START

    # Act: alternate silence and sustained speech; the speech frame resets the
    # silence accumulator each time, so a turn end never fires.
    for _ in range(10):
        assert ep.update(0.1) is None  # 32ms of silence
        assert ep.update(0.5) is None  # > sustain_threshold (0.35) -> reset


def test_endpointer_reset_clears_state() -> None:
    # Arrange: get into the speaking state.
    ep = Endpointer()
    ep.update(0.9)
    assert ep.update(0.9) is VadEvent.SPEECH_START

    # Act
    ep.reset()

    # Assert: after reset we are idle again — a single loud frame is not enough
    # to re-trigger SPEECH_START (proves the speaking flag and counters cleared).
    assert ep.update(0.9) is None
    assert ep.update(0.9) is VadEvent.SPEECH_START


def test_turn_end_resets_so_a_new_turn_can_start() -> None:
    # Arrange
    ep = Endpointer(silence_ms=64, frame_ms=32)
    ep.update(0.9)
    ep.update(0.9)  # SPEECH_START
    ep.update(0.1)  # 32ms silence
    assert ep.update(0.1) is VadEvent.TURN_END  # 64ms

    # Act / Assert: endpointer auto-reset on TURN_END, so a fresh turn starts.
    assert ep.update(0.9) is None
    assert ep.update(0.9) is VadEvent.SPEECH_START


# --- Endpointer: validation ----------------------------------------------------


def test_endpointer_rejects_start_below_sustain() -> None:
    with pytest.raises(ValueError, match="start_threshold must be >= sustain_threshold"):
        Endpointer(start_threshold=0.2, sustain_threshold=0.5)


def test_endpointer_rejects_nonpositive_frame_ms() -> None:
    with pytest.raises(ValueError, match="frame_ms must be positive"):
        Endpointer(frame_ms=0)


def test_endpointer_rejects_zero_start_frames() -> None:
    with pytest.raises(ValueError, match="speech_frames_to_start must be >= 1"):
        Endpointer(speech_frames_to_start=0)


# --- BargeInDetector -----------------------------------------------------------


def test_barge_in_requires_consecutive_speech_frames() -> None:
    # Arrange: default needs 4 consecutive frames above 0.6.
    det = BargeInDetector()

    # Act / Assert: first three loud frames do not confirm yet.
    assert det.update(0.9) is False
    assert det.update(0.9) is False
    assert det.update(0.9) is False
    # Fourth consecutive loud frame confirms barge-in.
    assert det.update(0.9) is True


def test_barge_in_counter_resets_on_quiet_frame() -> None:
    # Arrange
    det = BargeInDetector(speech_frames=3)

    # Act: a quiet frame in the middle breaks the run.
    assert det.update(0.9) is False
    assert det.update(0.9) is False
    assert det.update(0.2) is False  # resets
    assert det.update(0.9) is False
    assert det.update(0.9) is False
    assert det.update(0.9) is True


def test_barge_in_below_threshold_never_confirms() -> None:
    # Arrange
    det = BargeInDetector(threshold=0.6)

    # Act / Assert: frames at or below threshold never confirm.
    for _ in range(10):
        assert det.update(0.6) is False
        assert det.update(0.3) is False


def test_barge_in_reset_clears_counter() -> None:
    # Arrange
    det = BargeInDetector(speech_frames=2)
    assert det.update(0.9) is False

    # Act
    det.reset()

    # Assert: the partial run was cleared, so two fresh frames are needed again.
    assert det.update(0.9) is False
    assert det.update(0.9) is True


def test_barge_in_rejects_zero_speech_frames() -> None:
    with pytest.raises(ValueError, match="speech_frames must be >= 1"):
        BargeInDetector(speech_frames=0)
