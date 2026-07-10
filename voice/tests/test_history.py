"""Bounded session memory: append/trim keep follow-ups in context yet capped."""

from __future__ import annotations

from history import append_turn, est_tokens, trim


def _u(text: str) -> dict[str, str]:
    return {"role": "user", "content": text}


def _a(text: str) -> dict[str, str]:
    return {"role": "assistant", "content": text}


def test_est_tokens_is_chars_over_four() -> None:
    assert est_tokens([_u("a" * 8), _a("b" * 12)]) == 5  # (8 + 12) // 4


def test_append_turn_grows_history_and_keeps_order() -> None:
    h0: list[dict[str, str]] = []
    h1 = append_turn(h0, "events this weekend?", "You have brunch Sat.", 4000)
    h2 = append_turn(h1, "what about next week?", "Two meetings Mon.", 4000)
    assert [m["content"] for m in h2] == [
        "events this weekend?",
        "You have brunch Sat.",
        "what about next week?",
        "Two meetings Mon.",
    ]
    assert h0 == []  # pure: earlier lists are untouched


def test_append_turn_drops_blank_answer() -> None:
    h = append_turn([], "hello?", "   ", 4000)
    assert h == []  # an empty/errored turn must not enter context


def test_trim_drops_oldest_pair_when_over_budget() -> None:
    # Two turns, ~9 tokens each (36 chars // 4); budget of 10 tokens keeps only
    # the most recent pair.
    history = [
        _u("x" * 36), _a("y" * 36),   # oldest
        _u("z" * 36), _a("w" * 36),   # newest
    ]
    trimmed = trim(history, budget_tokens=10)
    assert trimmed == [_u("z" * 36), _a("w" * 36)]


def test_trim_never_drops_the_last_pair_even_if_huge() -> None:
    history = [_u("q" * 10_000), _a("a" * 10_000)]
    trimmed = trim(history, budget_tokens=10)
    assert trimmed == history  # a single long turn is kept, not wiped


def test_append_turn_stays_within_budget_over_many_turns() -> None:
    history: list[dict[str, str]] = []
    for i in range(20):
        history = append_turn(history, f"q{i} " + "x" * 200, f"a{i} " + "y" * 200, 500)
    assert est_tokens(history) <= 500
    # the freshest turn is always present
    assert history[-2]["content"].startswith("q19")
    assert history[-1]["content"].startswith("a19")
