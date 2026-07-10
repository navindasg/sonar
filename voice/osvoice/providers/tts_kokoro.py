"""Kokoro TTS adapter — mlx-audio TTS specialized with Kokoro-82M defaults.

Streams clause strings to PCM16 mono speech via mlx-audio's blocking Kokoro
generator. The model is loaded once (and pre-warmed) under the process-wide MLX
limiter, and every `generate` step is driven through `runtime.stream_sync`, so no
two MLX evals ever overlap. Heavy backends (mlx, mlx_audio) are imported lazily
inside methods, keeping this module cheap to import for the pure-logic tests.

Constructor takes the Kokoro *voice* name as its spec (default ``af_heart``); the
repo and language code are fixed Kokoro defaults. Output sample rate is read from
each result, never hardcoded (Kokoro emits 24 kHz).
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Iterator

import numpy as np

from osvoice.audio import float32_to_pcm16
from osvoice.runtime import offload

logger = logging.getLogger("osvoice.tts.kokoro")

_REPO = "mlx-community/Kokoro-82M-bf16"
_DEFAULT_VOICE = "af_heart"
_LANG_CODE = "a"
_STREAMING_INTERVAL = 0.4
_WARMUP_TEXT = "Ready."


class KokoroTTS:
    """`TTSProvider` backed by mlx-audio's Kokoro-82M model.

    Args:
        spec: Kokoro voice name (e.g. ``af_heart``). Empty falls back to default.
    """

    def __init__(self, spec: str = "") -> None:
        self._voice = spec.strip() or _DEFAULT_VOICE
        self._model: Any | None = None

    async def load(self) -> None:
        """Load Kokoro weights and run one warmup generation under MLX_LIMITER."""
        try:
            self._model = await offload(self._build_model)
            await offload(self._warmup)
        except Exception as error:  # noqa: BLE001 — surface a clear load failure
            self._model = None
            logger.exception("Failed to load Kokoro TTS model %s", _REPO)
            raise RuntimeError(
                f"Could not load Kokoro TTS model '{_REPO}': {error}"
            ) from error
        logger.info("Loaded Kokoro TTS %s (voice=%s)", _REPO, self._voice)

    def _build_model(self) -> Any:
        """Build the mlx-audio model (runs in a worker thread)."""
        from mlx_audio.tts.utils import load_model  # lazy heavy import

        return load_model(_REPO)

    def _warmup(self) -> None:
        """Run a throwaway generation so the first real clause is not cold."""
        for _ in self._generate(_WARMUP_TEXT):
            pass

    async def aclose(self) -> None:
        """Release the model reference. mlx frees Metal buffers on GC."""
        self._model = None

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        """Consume clause strings; yield PCM16 mono frames (Kokoro = 24 kHz)."""
        if self._model is None:
            raise RuntimeError("KokoroTTS.stream() called before load()")
        async for clause in text:
            clause = clause.strip()
            if not clause:
                continue
            async for chunk in self._stream_clause(clause):
                yield chunk

    async def _stream_clause(self, clause: str) -> AsyncIterator[bytes]:
        """Synthesize one clause atomically on the MLX thread, then emit its frames.

        All MLX work for the clause — driving the generator AND materializing each
        result's audio into PCM bytes — runs inside one ``offload()`` call on the
        MLX worker thread; only plain bytes cross back to the event loop. A lazy
        ``mx.array`` evaluated on the consumer thread raises ``There is no
        Stream(gpu, 0) in current thread``. Kokoro emits ~one chunk per sentence,
        so synthesizing a clause at a time loses nothing.
        """
        try:
            frames = await offload(self._synth_clause, clause)
        except Exception as error:  # noqa: BLE001 — clear inference failure
            logger.exception("Kokoro TTS generation failed for clause %r", clause)
            raise RuntimeError(f"Kokoro TTS generation failed: {error}") from error
        for pcm16 in frames:
            yield pcm16

    def _synth_clause(self, clause: str) -> list[bytes]:
        """Drain Kokoro's generator for one clause into PCM16 frames (MLX thread)."""
        frames: list[bytes] = []
        for result in self._generate(clause):
            pcm16 = _result_to_pcm16(result)
            if pcm16:
                frames.append(pcm16)
            if getattr(result, "is_final_chunk", False):
                break
        return frames

    def _generate(self, clause: str) -> Iterator[Any]:
        """Return Kokoro's per-sentence generator (runs in a worker thread).

        Non-streaming on purpose: mlx-audio's streaming path (stream=True +
        streaming_interval) slices each sentence into ~interval-sized chunks and
        concatenates them, which on this build throws ``[broadcast_shapes] ...
        cannot be broadcast`` when a clause's trailing slice is a different length
        — it killed even the short "One sec." ack. Generating each sentence whole
        avoids that concat entirely; we already synthesize one short clause at a
        time, so there's nothing to gain from sub-clause streaming.
        """
        return self._model.generate(  # type: ignore[union-attr]
            text=clause,
            voice=self._voice,
            lang_code=_LANG_CODE,
            stream=False,
        )


def _result_to_pcm16(result: Any) -> bytes:
    """Convert one mlx-audio result chunk to PCM16 bytes (empty if no audio)."""
    audio = getattr(result, "audio", None)
    if audio is None:
        return b""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return b""
    return float32_to_pcm16(samples)
