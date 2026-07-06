"""mlx-audio STT escape-hatch adapter (batch-oriented; offloaded).

A generic fallback over the mlx-audio STT zoo (Whisper, Parakeet, Voxtral, ...)
for models without a dedicated streaming adapter. We accumulate the entire PCM16
@16 kHz stream, decode it to a normalized float32 `mx.array`, run a single
blocking `generate()` under the process MLX limiter, and yield one final
`Transcript`. Whisper is batch-only; streaming-capable models (Parakeet/Voxtral)
are tried via `generate(..., stream=True)` and fall back to batch on `TypeError`.

Heavy, Apple-Silicon-only imports (mlx, mlx_audio) stay lazy inside methods so
the registry/resolver can import this module without those backends installed.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import numpy as np

from osvoice.audio import pcm16_to_float32
from osvoice.contracts import Transcript
from osvoice.runtime import offload

logger = logging.getLogger("osvoice.stt.mlxaudio")

# 0.1 s of silence at 16 kHz — enough to exercise the decode path on warmup.
INPUT_WARMUP_SAMPLES = 1_600


class MLXAudioSTT:
    """STTProvider backed by mlx-audio's batch STT loader.

    `spec` is a Hugging Face repo id, e.g. ``mlx-community/whisper-large-v3-turbo``.
    """

    def __init__(self, spec: str) -> None:
        self._repo_id = spec
        self._model: Any | None = None

    async def load(self) -> None:
        """Load the STT model once and warm it on a short silent buffer."""
        try:
            self._model = await offload(self._load_model)
        except Exception as exc:  # pragma: no cover - requires hardware
            logger.exception("Failed to load mlx-audio STT model %r", self._repo_id)
            raise RuntimeError(
                f"Could not load mlx-audio STT model {self._repo_id!r}: {exc}"
            ) from exc

        try:
            warmup = np.zeros(INPUT_WARMUP_SAMPLES, dtype=np.float32)
            await offload(self._generate, warmup)
            logger.info("Loaded and warmed mlx-audio STT model %r", self._repo_id)
        except Exception:  # pragma: no cover - requires hardware
            # Warmup is best-effort: a failed silent decode must not block startup.
            logger.warning("Warmup inference failed for %r", self._repo_id, exc_info=True)

    def _load_model(self) -> Any:
        """Build the model in a worker thread (lazy heavy import)."""
        from mlx_audio.stt.utils import load as load_stt

        return load_stt(self._repo_id)

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        """Accumulate the full PCM16 stream, decode once, yield a final Transcript."""
        if self._model is None:
            raise RuntimeError("MLXAudioSTT.stream called before load()")

        samples = await _collect_float32(audio)
        if samples.size == 0:
            yield Transcript(text="", is_final=True)
            return

        try:
            text = await offload(self._generate, samples)
        except Exception as exc:  # pragma: no cover - requires hardware
            logger.exception("mlx-audio STT inference failed for %r", self._repo_id)
            raise RuntimeError(f"mlx-audio STT inference failed: {exc}") from exc

        yield Transcript(text=text.strip(), is_final=True)

    def _generate(self, samples: np.ndarray) -> str:
        """Run a single blocking decode and return the joined transcript text.

        Tries streaming (`stream=True`) for capable models and falls back to a
        batch call when the model's `generate` does not accept that kwarg. Both
        paths run inside one offloaded turn, so the whole decode holds the MLX
        limiter exactly once.
        """
        import mlx.core as mx

        audio = mx.array(samples)
        try:
            chunks = self._model.generate(audio, stream=True)
            return _join_stream(chunks)
        except TypeError:
            result = self._model.generate(audio)
            return _result_text(result)

    async def aclose(self) -> None:
        """Drop the model reference; mlx-audio holds no OS handles to release."""
        self._model = None
        logger.info("Closed mlx-audio STT model %r", self._repo_id)


async def _collect_float32(audio: AsyncIterator[bytes]) -> np.ndarray:
    """Drain a PCM16 @16 kHz byte stream into one normalized float32 array."""
    chunks: list[np.ndarray] = []
    async for frame in audio:
        if frame:
            chunks.append(pcm16_to_float32(frame))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)


def _join_stream(chunks: Any) -> str:
    """Join the `.text` of streamed chunks into a single transcript string."""
    parts = [getattr(chunk, "text", "") or "" for chunk in chunks]
    return "".join(parts)


def _result_text(result: Any) -> str:
    """Extract transcript text from a batch `generate` result."""
    text = getattr(result, "text", None)
    if text is None:
        raise RuntimeError("mlx-audio STT result has no `text` attribute")
    return text
