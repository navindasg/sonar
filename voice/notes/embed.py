"""Speaker-embedding adapter (speechbrain ECAPA-TDNN, CPU).

One ~192-dim voice fingerprint per endpointed utterance, for diarize.py's
online clustering. `speechbrain/spkrec-ecapa-voxceleb` is small (~80 MB),
ungated (no HF token), and runs a 2-5 s utterance through CPU inference in
well under a second — no GPU contention with MLX (Parakeet/Kokoro).

Heavy imports (torch, speechbrain) are lazy inside load(), same discipline as
the osvoice providers, so importing this module costs nothing and the notes
test suite runs on any machine. Inference is blocking torch -> run via
asyncio.to_thread. A failed load or a failed embed degrades to None, which
diarize.py maps to "stick with the current speaker".
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

from osvoice.audio import pcm16_to_float32

log = logging.getLogger("sonar.notes.embed")

_DEFAULT_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
_SAMPLE_RATE = 16_000
_MIN_EMBED_SAMPLES = _SAMPLE_RATE // 4   # <250 ms has no stable speaker signal
_WINDOW_S = 1.5                          # per-window span for sub-utterance diarization
_HOP_S = 0.75                            # window stride (overlap smooths the boundary)


def _cache_dir() -> Path:
    default = Path.home() / ".cache" / "sonar" / "spkrec-ecapa"
    return Path(os.environ.get("SONAR_NOTES_EMBED_CACHE", default)).expanduser()


class EcapaEmbedder:
    """Voice-fingerprint slot: load() once, then embed() per utterance."""

    def __init__(self, source: str = _DEFAULT_SOURCE) -> None:
        self._source = source
        self._encoder: Any | None = None
        self._torch: Any | None = None

    @property
    def ready(self) -> bool:
        return self._encoder is not None

    async def load(self) -> None:
        """Fetch weights (first run) and load the encoder on a worker thread."""
        if self._encoder is not None:
            return
        log.info("loading ECAPA speaker encoder %s (first run downloads ~80MB)", self._source)
        self._encoder, self._torch = await asyncio.to_thread(self._load_blocking)
        log.info("ECAPA speaker encoder ready")

    def _load_blocking(self) -> tuple[Any, Any]:
        import torch
        from speechbrain.inference.speaker import EncoderClassifier

        encoder = EncoderClassifier.from_hparams(
            source=self._source,
            savedir=str(_cache_dir()),
            run_opts={"device": "cpu"},
        )
        return encoder, torch

    async def embed(self, pcm: bytes) -> np.ndarray | None:
        """PCM16 @16 kHz mono -> one embedding vector (None if not embeddable)."""
        if self._encoder is None:
            return None
        samples = pcm16_to_float32(pcm)
        if samples.size < _MIN_EMBED_SAMPLES:
            return None
        try:
            return await asyncio.to_thread(self._embed_blocking, samples)
        except Exception as exc:  # noqa: BLE001 — one bad utterance must not kill notes
            log.warning("speaker embedding failed: %s", exc)
            return None

    def _embed_blocking(self, samples: np.ndarray) -> np.ndarray:
        wav = self._torch.from_numpy(np.ascontiguousarray(samples)).float().unsqueeze(0)
        with self._torch.no_grad():
            emb = self._encoder.encode_batch(wav)
        return emb.squeeze().cpu().numpy().astype(np.float32)

    async def embed_windows(
        self, pcm: bytes, window_s: float = _WINDOW_S, hop_s: float = _HOP_S
    ) -> list[tuple[int, int, np.ndarray]]:
        """Embed overlapping windows across a long utterance.

        Returns ``(start_sample, end_sample, embedding)`` per window so the
        controller can detect a mid-utterance speaker change (diarize.split_runs)
        and cut the utterance where the voice changes. Returns [] when the
        encoder is unavailable or the audio is too short for two windows — the
        caller then falls back to a single whole-utterance embedding.
        """
        if self._encoder is None:
            return []
        samples = pcm16_to_float32(pcm)
        win, hop = int(window_s * _SAMPLE_RATE), int(hop_s * _SAMPLE_RATE)
        if hop <= 0 or samples.size < win + hop:
            return []
        spans = [(s, s + win) for s in range(0, samples.size - win + 1, hop)]
        # Cover any leftover tail so the end of the utterance isn't dropped.
        if spans and spans[-1][1] < samples.size - hop // 2:
            spans.append((samples.size - win, samples.size))
        try:
            embs = await asyncio.to_thread(self._embed_windows_blocking, samples, spans)
        except Exception as exc:  # noqa: BLE001 — degrade to whole-utterance embedding
            log.warning("windowed embedding failed: %s", exc)
            return []
        return [(s, e, emb) for (s, e), emb in zip(spans, embs)]

    def _embed_windows_blocking(
        self, samples: np.ndarray, spans: list[tuple[int, int]]
    ) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        for s, e in spans:
            wav = self._torch.from_numpy(np.ascontiguousarray(samples[s:e])).float().unsqueeze(0)
            with self._torch.no_grad():
                emb = self._encoder.encode_batch(wav)
            out.append(emb.squeeze().cpu().numpy().astype(np.float32))
        return out

    async def aclose(self) -> None:
        self._encoder = None
        self._torch = None
