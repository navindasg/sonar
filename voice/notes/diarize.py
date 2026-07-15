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
