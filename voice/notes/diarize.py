"""Online speaker assignment over utterance embeddings (pure numpy).

Each endpointed utterance gets one speaker embedding (embed.py); this module
turns the stream of embeddings into stable "S1", "S2", … labels with simple
online cosine clustering:

  - cosine similarity >= `threshold` to a known centroid -> that speaker, and
    the centroid absorbs the new embedding (running mean of L2-normalized
    vectors);
  - otherwise a NEW speaker — but only if the utterance is long enough
    (`min_new_seconds`) to trust its embedding: grunts and "yeah"s fall back to
    the current speaker instead of spawning phantom ones;
  - at `max_speakers` the best match wins regardless of threshold.

No embedding at all (backend unavailable) degrades to a single-speaker session
— the UI's manual reassignment still works.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_THRESHOLD = 0.40
DEFAULT_MIN_NEW_SECONDS = 1.0
DEFAULT_MAX_SPEAKERS = 8
# A speaker-change boundary is placed between two adjacent windows whose voice
# fingerprints are less alike than this. Higher = split more eagerly.
DEFAULT_SPLIT_THRESHOLD = 0.50


@dataclass(frozen=True)
class Assignment:
    """One utterance's speaker decision."""

    speaker: str          # "S1", "S2", …
    is_new: bool          # True iff this utterance created the speaker
    similarity: float     # cosine sim to the chosen centroid (1.0 for a new one)


def _unit(v: np.ndarray) -> np.ndarray:
    """L2-normalize a vector; zero vectors pass through unchanged."""
    arr = np.asarray(v, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 0.0 else arr


def split_runs(
    embeddings: list[np.ndarray],
    threshold: float = DEFAULT_SPLIT_THRESHOLD,
    min_run: int = 2,
) -> list[tuple[int, int]]:
    """Group ordered window embeddings into same-speaker runs.

    Walks the sequence of per-window voice fingerprints and cuts a boundary
    wherever two adjacent windows are less alike than ``threshold`` (a speaker
    change with little or no pause — the case the silence endpointer can't
    catch). Returns ``(start, end_exclusive)`` window-index ranges covering the
    whole sequence. Runs shorter than ``min_run`` windows are merged into a
    neighbour so a single noisy window can't spawn a spurious split.

    Pure (numpy only) so it unit-tests without the embedding backend.
    """
    n = len(embeddings)
    if n == 0:
        return []
    if n == 1:
        return [(0, 1)]
    units = [_unit(e) for e in embeddings]
    cuts: list[int] = [0]
    for i in range(1, n):
        if float(np.dot(units[i - 1], units[i])) < threshold:
            cuts.append(i)
    cuts.append(n)
    runs = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]
    runs = _merge_short_runs(runs, units, min_run)
    # A lone noisy window leaves two abutting runs whose means are still alike;
    # coalesce adjacent runs that are similar enough to be the same voice so a
    # blip never counts as a real speaker change.
    return _coalesce_similar(runs, units, threshold)


def _coalesce_similar(
    runs: list[tuple[int, int]], units: list[np.ndarray], threshold: float
) -> list[tuple[int, int]]:
    """Merge adjacent runs whose mean fingerprints are >= ``threshold`` alike."""
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i in range(len(runs) - 1):
            a, b = runs[i], runs[i + 1]
            if float(np.dot(_mean_unit(units, a), _mean_unit(units, b))) >= threshold:
                runs = runs[:i] + [(a[0], b[1])] + runs[i + 2:]
                changed = True
                break
    return runs


def _merge_short_runs(
    runs: list[tuple[int, int]], units: list[np.ndarray], min_run: int
) -> list[tuple[int, int]]:
    """Fold runs shorter than ``min_run`` windows into the more similar
    neighbour, then coalesce adjacent runs (a short run between two long ones can
    leave two abutting ranges). Keeps only confident speaker changes."""
    if len(runs) <= 1:
        return runs
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i, (s, e) in enumerate(runs):
            if e - s >= min_run:
                continue
            # too short to trust as its own speaker: merge left or right, toward
            # whichever neighbour's mean embedding it resembles more.
            left = runs[i - 1] if i > 0 else None
            right = runs[i + 1] if i + 1 < len(runs) else None
            target = _closer_neighbour((s, e), left, right, units)
            merged = (min(target[0], s), max(target[1], e))
            runs = [r for j, r in enumerate(runs) if r not in ((s, e), target)]
            runs.insert(min(i, len(runs)), merged)
            runs.sort()
            changed = True
            break
    return runs


def _mean_unit(units: list[np.ndarray], span: tuple[int, int]) -> np.ndarray:
    return _unit(np.mean(units[span[0]:span[1]], axis=0))


def _closer_neighbour(
    span: tuple[int, int],
    left: tuple[int, int] | None,
    right: tuple[int, int] | None,
    units: list[np.ndarray],
) -> tuple[int, int]:
    """Pick the neighbour whose mean fingerprint the short span resembles more."""
    me = _mean_unit(units, span)
    scores = []
    if left is not None:
        scores.append((float(np.dot(me, _mean_unit(units, left))), left))
    if right is not None:
        scores.append((float(np.dot(me, _mean_unit(units, right))), right))
    return max(scores)[1]


class SpeakerRegistry:
    """Accumulates speaker centroids and assigns each utterance a label."""

    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        min_new_seconds: float = DEFAULT_MIN_NEW_SECONDS,
        max_speakers: int = DEFAULT_MAX_SPEAKERS,
    ) -> None:
        if not 0.0 < threshold < 1.0:
            raise ValueError("threshold must be in (0, 1)")
        if max_speakers < 1:
            raise ValueError("max_speakers must be >= 1")
        self._threshold = threshold
        self._min_new_seconds = min_new_seconds
        self._max_speakers = max_speakers
        self._sums: list[np.ndarray] = []   # per-speaker sum of unit embeddings
        self._counts: list[int] = []
        self._last: str | None = None

    @property
    def speakers(self) -> list[str]:
        """Known speaker ids, in first-heard order."""
        return [f"S{i + 1}" for i in range(len(self._sums))]

    def assign(self, embedding: np.ndarray | None, duration_s: float) -> Assignment:
        """Label one utterance and update the centroids."""
        if embedding is None or np.asarray(embedding).size == 0:
            return self._fallback()
        emb = _unit(embedding)
        if not self._sums:
            return self._create(emb)

        sims = [float(np.dot(emb, _unit(s))) for s in self._sums]
        best = int(np.argmax(sims))
        if sims[best] >= self._threshold:
            return self._absorb(best, emb, sims[best])
        if duration_s < self._min_new_seconds:
            # Too short to trust: stick with the current speaker, but do NOT
            # pollute their centroid with an uncertain embedding.
            return self._fallback(similarity=sims[best])
        if len(self._sums) >= self._max_speakers:
            return self._absorb(best, emb, sims[best])
        return self._create(emb)

    def _absorb(self, idx: int, emb: np.ndarray, sim: float) -> Assignment:
        self._sums[idx] = self._sums[idx] + emb
        self._counts[idx] += 1
        self._last = f"S{idx + 1}"
        return Assignment(speaker=self._last, is_new=False, similarity=sim)

    def _create(self, emb: np.ndarray) -> Assignment:
        self._sums.append(emb.copy())
        self._counts.append(1)
        self._last = f"S{len(self._sums)}"
        return Assignment(speaker=self._last, is_new=True, similarity=1.0)

    def _fallback(self, similarity: float = 0.0) -> Assignment:
        """No usable embedding: current speaker, or S1 for the very first turn.

        We deliberately do NOT seed a centroid here. A placeholder would have the
        wrong dimensionality (we never saw a real embedding), and the next real
        192-D embedding's cosine comparison against it would raise in np.dot and
        drop that segment. Leaving _sums empty lets a later real embedding create
        the first genuine centroid via assign()'s empty-registry path.
        """
        if self._last is None:
            self._last = "S1"
            return Assignment(speaker="S1", is_new=True, similarity=similarity)
        return Assignment(speaker=self._last, is_new=False, similarity=similarity)
