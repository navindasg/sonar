"""Rotating 'thinking' acks for the voice loop.

The ack is the short line spoken the instant a turn starts, to cover the harness's
blocking tool loop (~8 s on tool turns) so voice turns never open with dead air.
A single fixed phrase ("One sec." every time) gets grating fast, so we rotate a
pool and avoid repeating the previous one back-to-back.

Pure + tiny so it unit-tests without the audio stack (same split as history.py /
echo_gate.py). A fixed ack can still be forced via SONAR_VOICE_ACK in voice_loop.
"""
from __future__ import annotations

import random

# Short (~1 s), natural, request-agnostic. Punctuation kept plain (commas, no
# em-dashes) so Kokoro's G2P renders them cleanly.
ACKS: tuple[str, ...] = (
    "On it.",
    "Let me check.",
    "One sec.",
    "Looking now.",
    "Give me a sec.",
    "Checking.",
    "Let me see.",
    "Hang on.",
    "Got it, checking.",
    "Right away.",
    "Let me look into that.",
    "Sure, one moment.",
)


def next_ack(previous: str | None, rng: random.Random | None = None) -> str:
    """Pick a random ack, never repeating ``previous`` back-to-back.

    ``rng`` is injectable so tests are deterministic; defaults to the module's
    shared generator. With a single-item pool it returns that item.
    """
    r = rng or random
    choices = [a for a in ACKS if a != previous] or list(ACKS)
    return r.choice(choices)
