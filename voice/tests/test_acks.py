"""Rotating acks: valid picks, no back-to-back repeat, deterministic under a seed."""

from __future__ import annotations

import random

from acks import ACKS, next_ack


def test_pick_is_always_from_the_pool() -> None:
    rng = random.Random(0)
    for _ in range(50):
        assert next_ack(None, rng) in ACKS


def test_never_repeats_the_previous_ack() -> None:
    rng = random.Random(1)
    prev = ACKS[0]
    for _ in range(200):
        pick = next_ack(prev, rng)
        assert pick != prev          # back-to-back repeat is what got grating
        prev = pick


def test_rotation_actually_varies() -> None:
    # Over many turns the pool should exercise more than a couple of phrases.
    rng = random.Random(2)
    prev = None
    seen = set()
    for _ in range(100):
        prev = next_ack(prev, rng)
        seen.add(prev)
    assert len(seen) >= 5


def test_single_item_pool_returns_it(monkeypatch) -> None:
    import acks as acks_mod
    monkeypatch.setattr(acks_mod, "ACKS", ("Only one.",))
    assert acks_mod.next_ack("Only one.", random.Random(3)) == "Only one."
