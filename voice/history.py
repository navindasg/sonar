"""Bounded conversation memory for one voice session (pure — no audio stack).

While the overlay is open (one F5 session), prior turns ride along with each new
question so follow-ups resolve against context — ask "what's on my calendar this
weekend?" then "what about next week?" and the second turn still knows the
subject. The window is bounded by a rough token budget (~4 chars/token, no
tokenizer dependency) so it never grows without limit: the oldest user+assistant
pair is dropped first. A new session (overlay re-open) starts from empty.

Kept separate from ``voice_loop.py`` — which imports mlx/torch/sounddevice — so
this logic unit-tests on its own, the same way ``echo_gate`` and ``harness_client``
hold their pure cores apart from the IO.
"""
from __future__ import annotations

Message = dict[str, str]

_CHARS_PER_TOKEN = 4


def est_tokens(messages: list[Message]) -> int:
    """Cheap token estimate for a conversation (chars/4, no tokenizer)."""
    return sum(len(m.get("content", "")) for m in messages) // _CHARS_PER_TOKEN


def trim(history: list[Message], budget_tokens: int) -> list[Message]:
    """Drop the oldest user+assistant pair(s) until ``history`` fits the budget.

    Always keeps the most recent pair, so a single very long turn is never
    dropped mid-session. Pure: returns a new list, never mutates the input.
    """
    trimmed = history
    while len(trimmed) > 2 and est_tokens(trimmed) > budget_tokens:
        trimmed = trimmed[2:]  # one turn = one user + one assistant message
    return trimmed


def append_turn(
    history: list[Message], user_text: str, answer: str, budget_tokens: int
) -> list[Message]:
    """Return ``history`` + this turn, bounded to the token budget.

    A blank/empty answer is not remembered — an errored turn must not poison the
    next follow-up's context. Pure: the input list is never mutated.
    """
    if not answer.strip():
        return history
    return trim(
        history
        + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": answer},
        ],
        budget_tokens,
    )
