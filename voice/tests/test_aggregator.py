"""Tests for ClauseAggregator and the async aggregate() driver.

Covers sentence mode (emit on terminal punctuation past min_chars, buffer short
fragments, never split on abbreviations / decimals / ellipses), token mode
(forward each delta), flush() returning the tail, and the async aggregate()
helper that drives the aggregator over a delta stream and flushes the remainder.
Pure stdlib logic — no heavy backends involved.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest

from osvoice.aggregator import ClauseAggregator, aggregate


async def _stream(deltas: list[str]) -> AsyncIterator[str]:
    """Yield each delta as an async iterator (no real awaiting needed)."""
    for delta in deltas:
        yield delta


# --- construction / validation -------------------------------------------------


def test_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown aggregator mode"):
        ClauseAggregator(mode="paragraph")


def test_rejects_negative_min_chars() -> None:
    with pytest.raises(ValueError, match="min_chars must be non-negative"):
        ClauseAggregator(min_chars=-1)


# --- sentence mode -------------------------------------------------------------


def test_sentence_emits_on_terminal_punctuation_past_min_chars() -> None:
    # Arrange: a sentence comfortably longer than the default 24-char minimum.
    agg = ClauseAggregator(mode="sentence")

    # Act: the boundary only "closes" when followed by whitespace/end, so push a
    # trailing space after the period.
    out = agg.push("The weather is lovely today. ")

    # Assert: one complete clause emitted, trailing space stripped.
    assert out == ["The weather is lovely today."]


def test_sentence_buffers_short_fragment_until_long_enough() -> None:
    # Arrange
    agg = ClauseAggregator(mode="sentence", min_chars=24)

    # Act: a short sentence under the min should NOT emit yet...
    first = agg.push("Hi there. ")
    # ...but once enough text accumulates past a later boundary, it emits the
    # whole buffered run up to that boundary.
    second = agg.push("How are you doing this fine morning? ")

    # Assert
    assert first == []
    assert second == ["Hi there. How are you doing this fine morning?"]


def test_sentence_emits_across_multiple_token_deltas() -> None:
    # Arrange: the real usage pattern — many tiny deltas.
    agg = ClauseAggregator(mode="sentence")
    deltas = ["The", " quick", " brown", " fox", " jumps", " over", " now.", " "]

    # Act
    emitted: list[str] = []
    for delta in deltas:
        emitted.extend(agg.push(delta))

    # Assert
    assert emitted == ["The quick brown fox jumps over now."]


def test_sentence_does_not_split_on_abbreviation() -> None:
    # Arrange: "Dr." is an abbreviation, not a sentence end.
    agg = ClauseAggregator(mode="sentence")

    # Act: feed a clause that contains "Dr." mid-sentence and a real end after.
    emitted: list[str] = []
    emitted.extend(agg.push("Please go and see Dr. "))
    emitted.extend(agg.push("Smith about the results soon. "))

    # Assert: a single clause spanning the abbreviation, split only at the period.
    assert emitted == ["Please go and see Dr. Smith about the results soon."]


def test_sentence_does_not_split_on_decimal_number() -> None:
    # Arrange: "3.14" must stay intact.
    agg = ClauseAggregator(mode="sentence")

    # Act
    emitted: list[str] = []
    emitted.extend(agg.push("The value of pi is roughly 3.14 in most cases. "))

    # Assert: the decimal did not cause an early cut.
    assert emitted == ["The value of pi is roughly 3.14 in most cases."]


def test_sentence_does_not_split_on_ellipsis() -> None:
    # Arrange: ellipsis is mid-thought.
    agg = ClauseAggregator(mode="sentence")

    # Act
    emitted: list[str] = []
    emitted.extend(agg.push("Well I was thinking about it... "))
    emitted.extend(agg.push("and then I changed my mind entirely. "))

    # Assert: only the final period cut the clause.
    assert emitted == [
        "Well I was thinking about it... and then I changed my mind entirely."
    ]


def test_sentence_splits_on_clause_separator_past_min_chars() -> None:
    # Arrange: commas are softer clause boundaries; still gated by min_chars.
    agg = ClauseAggregator(mode="sentence", min_chars=10)

    # Act
    out = agg.push("First a long enough clause, and then more. ")

    # Assert: two cuts — at the comma and at the period.
    assert out == ["First a long enough clause,", "and then more."]


def test_flush_returns_buffered_tail() -> None:
    # Arrange: text with no closing boundary stays buffered.
    agg = ClauseAggregator(mode="sentence")
    assert agg.push("an unfinished thought with no end") == []

    # Act
    tail = agg.flush()

    # Assert: flush returns the stripped remainder and clears the buffer.
    assert tail == ["an unfinished thought with no end"]
    assert agg.flush() == []


def test_empty_delta_is_ignored() -> None:
    agg = ClauseAggregator(mode="sentence")
    assert agg.push("") == []


# --- token mode ----------------------------------------------------------------


def test_token_mode_forwards_each_delta() -> None:
    # Arrange
    agg = ClauseAggregator(mode="token")

    # Act / Assert: every non-empty delta is emitted immediately, verbatim.
    assert agg.push("hello") == ["hello"]
    assert agg.push(" world") == [" world"]
    assert agg.push("") == []
    # Token mode buffers nothing, so flush yields nothing.
    assert agg.flush() == []


# --- async aggregate() ---------------------------------------------------------


async def test_aggregate_yields_clauses_then_flushes_tail() -> None:
    # Arrange: two complete sentences plus a trailing unterminated fragment.
    deltas = [
        "The morning sun is warm today. ",
        "Birds are singing in the trees nearby. ",
        "and one last partial bit",
    ]

    # Act
    clauses = [clause async for clause in aggregate(_stream(deltas))]

    # Assert: both complete sentences emitted in order, then the flushed tail.
    assert clauses == [
        "The morning sun is warm today.",
        "Birds are singing in the trees nearby.",
        "and one last partial bit",
    ]


async def test_aggregate_token_mode_passes_through() -> None:
    # Arrange
    deltas = ["a", "b", "c"]

    # Act
    out = [d async for d in aggregate(_stream(deltas), mode="token")]

    # Assert
    assert out == ["a", "b", "c"]


async def test_aggregate_propagates_stream_error_after_flush() -> None:
    # Arrange: a stream that raises mid-way; aggregate must re-raise.
    async def boom() -> AsyncIterator[str]:
        yield "partial start no boundary "
        raise RuntimeError("upstream LLM failed")

    # Act / Assert
    with pytest.raises(RuntimeError, match="upstream LLM failed"):
        async for _ in aggregate(boom()):
            pass


async def test_aggregate_aclose_midstream_does_not_raise() -> None:
    # Regression: on barge-in the TTS consumer stops early and aclose()s the
    # aggregate generator, throwing GeneratorExit. The trailing flush must NOT
    # run inside a `finally` that yields, or aclose() raises RuntimeError.
    async def endless() -> AsyncIterator[str]:
        # First delta emits one clause (so __anext__ returns) and leaves a
        # non-empty trailing buffer; then keep streaming boundary-free text.
        yield "First full clause sentence here. partial tail with no boundary"
        while True:
            yield " still flowing with no boundary"

    gen = aggregate(endless())
    first = await gen.__anext__()
    assert first == "First full clause sentence here."

    # Closing mid-stream (mid barge-in) must complete cleanly — no RuntimeError
    # "async generator ignored GeneratorExit".
    await gen.aclose()
