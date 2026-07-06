"""mlx-audio TTS escape-hatch adapter (Kokoro and friends).

Wraps any `mlx_audio.tts` model behind the `TTSProvider` slot protocol. Heavy,
Apple-Silicon-only imports (`mlx`, `mlx_audio`) live inside `load()`/`stream()`
so importing this module — and the registry that walks it — stays cheap and
works without those backends installed.

Every blocking MLX call serializes on the process-wide `MLX_LIMITER`: the model
load runs through `runtime.offload`, and the blocking streaming generator is
driven step-by-step through `runtime.stream_sync`. `model.generate(...)` is
always a generator, so we iterate it even when not streaming. Output sample rate
is read off each result (Kokoro is 24 kHz) — never hardcoded.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Iterator

import numpy as np

from osvoice import runtime
from osvoice.audio import float32_to_pcm16
from osvoice.contracts import OUTPUT_SAMPLE_RATE

logger = logging.getLogger("osvoice.tts.mlxaudio")

# Default voice for Kokoro-style models; overridable via the constructor.
_DEFAULT_VOICE = "af_heart"
# Chunk granularity for the model's internal streaming, in seconds.
_STREAMING_INTERVAL = 0.4


class MLXAudioTTS:
    """`TTSProvider` backed by an `mlx_audio.tts` model loaded by repo id."""

    def __init__(self, spec: str, voice: str = _DEFAULT_VOICE) -> None:
        if not spec:
            raise ValueError("MLXAudioTTS requires a non-empty model repo id")
        self._repo_id = spec
        self._voice = voice
        self._model: object | None = None
        # Best-effort default until the first result reports the real rate.
        self.sample_rate = OUTPUT_SAMPLE_RATE

    async def load(self) -> None:
        """Load the TTS model and run one warmup generation."""
        try:
            self._model = await runtime.offload(self._load_model)
        except Exception as exc:  # noqa: BLE001 - surface a clear load failure
            logger.exception("Failed to load mlx-audio TTS model %r", self._repo_id)
            raise RuntimeError(
                f"mlx-audio TTS model load failed for {self._repo_id!r}: {exc}"
            ) from exc

        try:
            await runtime.offload(self._synth_clause, "Hello.")  # warmup inference
        except Exception as exc:  # noqa: BLE001 - warmup must surface real errors
            logger.exception("mlx-audio TTS warmup failed for %r", self._repo_id)
            raise RuntimeError(
                f"mlx-audio TTS warmup failed for {self._repo_id!r}: {exc}"
            ) from exc

        logger.info("Loaded mlx-audio TTS model %r (voice=%r)", self._repo_id, self._voice)

    def _load_model(self) -> object:
        """Blocking model load (runs on the MLX worker thread)."""
        from mlx_audio.tts.utils import load_model

        return load_model(self._repo_id)

    async def aclose(self) -> None:
        """Drop the model reference so its buffers can be released."""
        self._model = None

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        """Synthesize each incoming clause and yield its PCM16 mono frames."""
        if self._model is None:
            raise RuntimeError("MLXAudioTTS.stream called before load()")

        async for clause in text:
            if not clause or not clause.strip():
                continue
            try:
                frames = await runtime.offload(self._synth_clause, clause)
            except Exception as exc:  # noqa: BLE001 - one clause must not be silent
                logger.exception("mlx-audio TTS synthesis failed for clause %r", clause)
                raise RuntimeError(f"mlx-audio TTS synthesis failed: {exc}") from exc
            for chunk in frames:
                yield chunk

    def _synth_clause(self, clause: str) -> list[bytes]:
        """Synthesize one clause to PCM16 frames entirely on the MLX thread.

        Every MLX touch — driving the generator AND materializing each result's
        audio (np.asarray forces evaluation of the lazy mx.array) — must happen
        here on the worker thread; only plain bytes cross back to the event loop.
        Evaluating an mx.array on the consumer thread raises
        ``There is no Stream(gpu, 0) in current thread``.
        """
        frames: list[bytes] = []
        for result in self._make_generator(clause):
            rate = getattr(result, "sample_rate", None)
            if rate:
                self.sample_rate = int(rate)
            samples = np.asarray(result.audio, dtype=np.float32)
            if samples.size:
                frames.append(float32_to_pcm16(samples))
        return frames

    def _make_generator(self, clause: str) -> Iterator[object]:
        """Build the blocking mlx-audio generator (runs on the MLX worker thread)."""
        assert self._model is not None  # guarded by callers
        return self._model.generate(  # type: ignore[attr-defined]
            text=clause,
            voice=self._voice,
            stream=True,
            streaming_interval=_STREAMING_INTERVAL,
        )
