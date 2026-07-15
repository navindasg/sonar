"""Online speaker clustering: stable labels, short-utterance fallback, caps."""

from __future__ import annotations

import numpy as np
import pytest

from notes.diarize import Assignment, SpeakerRegistry

A = np.array([1.0, 0.0, 0.0], dtype=np.float32)
B = np.array([0.0, 1.0, 0.0], dtype=np.float32)
C = np.array([0.0, 0.0, 1.0], dtype=np.float32)
A_ISH = np.array([0.97, 0.24, 0.0], dtype=np.float32)   # cos(A, A_ISH) ~ 0.97


def test_first_voice_becomes_speaker_one() -> None:
    reg = SpeakerRegistry()
    a = reg.assign(A, duration_s=2.0)
    assert a == Assignment(speaker="S1", is_new=True, similarity=1.0)
    assert reg.speakers == ["S1"]


def test_same_voice_reuses_the_label() -> None:
    reg = SpeakerRegistry()
    reg.assign(A, duration_s=2.0)
    a = reg.assign(A_ISH, duration_s=2.0)
    assert a.speaker == "S1" and not a.is_new
    assert a.similarity > 0.9


def test_distinct_voice_creates_speaker_two() -> None:
    reg = SpeakerRegistry()
    reg.assign(A, duration_s=2.0)
    b = reg.assign(B, duration_s=2.0)
    assert b.speaker == "S2" and b.is_new
    assert reg.speakers == ["S1", "S2"]


def test_short_unmatched_utterance_sticks_with_current_speaker() -> None:
    # A grunt/"yeah" too short to trust must never spawn a phantom speaker.
    reg = SpeakerRegistry(min_new_seconds=1.0)
    reg.assign(A, duration_s=2.0)
    short = reg.assign(B, duration_s=0.4)
    assert short.speaker == "S1" and not short.is_new
    # ...and it must not have polluted S1's centroid: a real B still splits off.
    real = reg.assign(B, duration_s=2.0)
    assert real.speaker == "S2" and real.is_new


def test_missing_embedding_falls_back_to_current_speaker() -> None:
    reg = SpeakerRegistry()
    first = reg.assign(None, duration_s=2.0)   # embedder down from the start
    assert first.speaker == "S1"
    again = reg.assign(None, duration_s=2.0)
    assert again.speaker == "S1" and not again.is_new


def test_speaker_cap_assigns_best_match_instead_of_growing() -> None:
    reg = SpeakerRegistry(max_speakers=2)
    reg.assign(A, duration_s=2.0)
    reg.assign(B, duration_s=2.0)
    c = reg.assign(C, duration_s=2.0)
    assert c.speaker in ("S1", "S2") and not c.is_new
    assert reg.speakers == ["S1", "S2"]


def test_centroid_tracks_the_running_mean() -> None:
    reg = SpeakerRegistry()
    reg.assign(A, duration_s=2.0)
    reg.assign(A_ISH, duration_s=2.0)
    # The centroid moved toward A_ISH, so an A_ISH-ish voice still matches S1.
    again = reg.assign(A_ISH, duration_s=2.0)
    assert again.speaker == "S1"


@pytest.mark.parametrize("kwargs", [
    {"threshold": 0.0}, {"threshold": 1.0}, {"max_speakers": 0},
])
def test_bad_config_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        SpeakerRegistry(**kwargs)


def test_none_first_then_real_embedding_does_not_crash_or_drop() -> None:
    # #6 regression: a first None (embedder not ready) used to plant a 1-D zero
    # placeholder centroid; the next REAL 192-D embedding's np.dot against it
    # would raise a shape error and that segment would be dropped. The None turn
    # must fall back to S1 WITHOUT seeding a bogus centroid, so the following
    # real embedding creates the genuine first centroid and is never lost.
    rng = np.random.default_rng(0)
    v1 = rng.standard_normal(192).astype(np.float32)
    v1_ish = (v1 + 0.01 * rng.standard_normal(192).astype(np.float32)).astype(np.float32)
    v2 = rng.standard_normal(192).astype(np.float32)   # a clearly different voice

    reg = SpeakerRegistry()
    a0 = reg.assign(None, duration_s=2.0)              # embedder down for turn 0
    a1 = reg.assign(v1, duration_s=2.0)                # first REAL embedding
    a2 = reg.assign(v1_ish, duration_s=2.0)            # same voice
    a3 = reg.assign(v2, duration_s=2.0)                # different voice

    # every turn got a label — the real embedding after the None was NOT dropped
    assert [a.speaker for a in (a0, a1, a2, a3)] == ["S1", "S1", "S1", "S2"]
    assert reg.speakers == ["S1", "S2"]
