"""Clause aggregator: buffer LLM token deltas into clause/sentence strings.

The LLM yields a stream of tiny token deltas, but TTS wants chunks that are long
enough to sound natural and short enough to start speaking quickly. This module
hand-rolls the same idea as Pipecat's SimpleTextAggregator: accumulate deltas and
flush a clause at a sentence/clause boundary once it is long enough, while
guarding against false boundaries (abbreviations, decimals, ellipses).

Pure and synchronous so it is trivial to unit-test; the async `aggregate` helper
drives it over a delta stream. No mlx / heavy deps here — stdlib + logging only.
"""
from __future__ import annotations

import logging
import re
from typing import AsyncIterator, Final

logger = logging.getLogger("osvoice.aggregator")

# Terminal sentence punctuation and softer clause separators. We emit on either,
# but only once the trimmed clause is long enough to be worth speaking.
_SENTENCE_END: Final = frozenset(".?!")
_CLAUSE_SEP: Final = frozenset(",;:")
_BOUNDARY: Final = _SENTENCE_END | _CLAUSE_SEP

# A boundary is a run of trailing boundary chars (e.g. "?!", "...") followed by
# whitespace or end-of-buffer. We only cut when the boundary is "closed" by
# whitespace so we never split a token mid-flight.
_BOUNDARY_RE: Final = re.compile(r"[.?!,;:]+(?=\s|$)")

# Tokens ending in "." that are almost always mid-sentence, not a real stop.
_ABBREVIATIONS: Final = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "st",
        "sr",
        "jr",
        "vs",
        "inc",
        "ltd",
        "co",
        "corp",
        "no",
        "etc",
        "e.g",
        "i.e",
        "a.m",
        "p.m",
    }
)


def _last_word(text: str) -> str:
    """Return the trailing alpha/dot run preceding the boundary, lowercased."""
    match = re.search(r"([A-Za-z][A-Za-z.]*)$", text)
    return match.group(1).lower().rstrip(".") if match else ""


def _is_false_boundary(buffer: str, end: int) -> bool:
    """True when the boundary char at ``end - 1`` should NOT split the buffer.

    Guards against abbreviations (Dr.), decimal numbers (3.14) and ellipses
    (...). ``end`` is the index just past the matched boundary run.
    """
    head = buffer[:end]
    boundary_char = buffer[end - 1]
    if boundary_char != ".":
        return False

    # Ellipsis ("...") is mid-thought, not a sentence end.
    if head.endswith("..."):
        return True

    # Decimal number: a digit on both sides of a single dot (3.14).
    if end < len(buffer) and head[-1] == "." and head[:-1].endswith(tuple("0123456789")):
        if buffer[end].isdigit():
            return True

    # Known abbreviation immediately before the dot (Dr., e.g.).
    return _last_word(head[:-1] if head.endswith(".") else head) in _ABBREVIATIONS


class ClauseAggregator:
    """Accumulate token deltas and emit complete clauses for TTS.

    ``mode="token"`` forwards each delta immediately (lowest latency).
    ``mode="sentence"`` buffers until a real boundary lands and the trimmed
    clause is at least ``min_chars`` long. The buffer is private; the only way
    to observe it is via the strings returned by :meth:`push` / :meth:`flush`.
    """

    def __init__(self, mode: str = "sentence", min_chars: int = 24) -> None:
        if mode not in ("sentence", "token"):
            raise ValueError(f"unknown aggregator mode: {mode!r}")
        if min_chars < 0:
            raise ValueError(f"min_chars must be non-negative, got {min_chars}")
        self._mode = mode
        self._min_chars = min_chars
        self._buffer = ""

    def push(self, delta: str) -> list[str]:
        """Append ``delta``; return any newly completed clauses (maybe empty)."""
        if not delta:
            return []
        if self._mode == "token":
            return [delta]

        self._buffer = self._buffer + delta
        return self._drain()

    def _drain(self) -> list[str]:
        """Cut every complete, long-enough clause from the front of the buffer."""
        clauses: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            clause = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut:].lstrip()
            if clause:
                clauses.append(clause)
        return clauses

    def _find_cut(self) -> int | None:
        """Index to cut the buffer at, or None if no valid boundary yet.

        Walks each candidate boundary, skipping false ones; a real boundary only
        cuts when the clause up to it is at least ``min_chars`` long.
        """
        for match in _BOUNDARY_RE.finditer(self._buffer):
            end = match.end()
            if _is_false_boundary(self._buffer, end):
                continue
            if len(self._buffer[:end].strip()) >= self._min_chars:
                return end
        return None

    def flush(self) -> list[str]:
        """Return remaining non-empty text and clear the buffer (end of stream)."""
        remainder = self._buffer.strip()
        self._buffer = ""
        return [remainder] if remainder else []


async def aggregate(
    deltas: AsyncIterator[str],
    mode: str = "sentence",
    min_chars: int = 24,
) -> AsyncIterator[str]:
    """Drive a :class:`ClauseAggregator` over ``deltas``, yielding clauses.

    Flushes any trailing buffer once the delta stream is exhausted, so the final
    partial sentence still reaches TTS.
    """
    aggregator = ClauseAggregator(mode=mode, min_chars=min_chars)
    try:
        async for delta in deltas:
            for clause in aggregator.push(delta):
                yield clause
        # Flush the trailing clause on NORMAL completion only. Never yield from a
        # `finally`: on barge-in the consumer aclose()s this generator, throwing
        # GeneratorExit here, and yielding during it raises RuntimeError.
        for clause in aggregator.flush():
            yield clause
    except Exception:
        logger.exception("aggregate: delta stream failed")
        raise
