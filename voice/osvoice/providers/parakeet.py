"""Parakeet STT adapter (parakeet-mlx, Apple Silicon).

Transcribes an endpointed utterance with parakeet-mlx. The whole `transcribe_stream`
session — open the context, feed every PCM16 chunk, read the result, close — runs
inside a SINGLE `runtime.offload()` call so all MLX evaluation stays on the one MLX
worker thread (an mx.array must be evaluated on the thread that created it; doing so
elsewhere raises ``std::runtime_error: There is no Stream(gpu, 0) in current
thread``). For a file or an endpointed turn we have the full audio up front, so
one-shot transcription is exactly right; live incremental partials are a later
enhancement (P5).

Heavy imports (`mlx`, `parakeet_mlx`) are lazy, inside `load()`/the worker helpers,
so importing this adapter and the registry works without the backend installed.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from osvoice.audio import pcm16_to_float32
from osvoice.contracts import Transcript
from osvoice.runtime import offload

logger = logging.getLogger("osvoice.parakeet")

_DEFAULT_SPEC = "mlx-community/parakeet-tdt-0.6b-v3"
_CONTEXT_SIZE = (256, 256)
_WARMUP_SAMPLES = 16_000  # 1 s of silence at the 16 kHz input rate


class ParakeetMLX:
    """STT slot backed by a parakeet-mlx model.

    `spec` is the model repo id; the default points at the v3 TDT checkpoint.
    `load()` fetches weights and runs one warmup pass; `stream()` consumes a PCM16
    @16 kHz audio stream and yields a single final `Transcript` on endpoint.
    """

    def __init__(self, spec: str = _DEFAULT_SPEC) -> None:
        self._spec = spec or _DEFAULT_SPEC
        self._model: Any | None = None

    async def load(self) -> None:
        """Load the model weights and run one warmup transcription."""
        logger.info("loading parakeet model %s", self._spec)
        try:
            self._model = await offload(self._load_model)
            await offload(self._warmup)
        except Exception as exc:  # pragma: no cover - exercised only with backend
            self._model = None
            logger.exception("failed to load parakeet model %s", self._spec)
            raise RuntimeError(f"parakeet load failed for {self._spec!r}: {exc}") from exc
        logger.info("parakeet model %s ready", self._spec)

    def _load_model(self) -> Any:
        """Blocking weight load (runs on the MLX worker thread)."""
        from parakeet_mlx import from_pretrained

        return from_pretrained(self._spec)

    def _warmup(self) -> None:
        """Blocking warmup pass over a second of silence to prime the graph."""
        import mlx.core as mx

        model = self._require_model()
        with model.transcribe_stream(context_size=_CONTEXT_SIZE) as tx:
            tx.add_audio(mx.zeros(_WARMUP_SAMPLES))

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        """Collect the utterance, transcribe it in one MLX call, yield the final text."""
        model = self._require_model()
        chunks: list[bytes] = []
        async for chunk in audio:
            if chunk:
                chunks.append(chunk)
        if not chunks:
            yield Transcript(text="", is_final=True)
            return
        try:
            text = await offload(self._transcribe_all, model, chunks)
        except Exception as exc:
            logger.exception("parakeet transcription failed")
            raise RuntimeError(f"parakeet transcription failed: {exc}") from exc
        yield Transcript(text=text.strip(), is_final=True)

    @staticmethod
    def _transcribe_all(model: Any, chunks: list[bytes]) -> str:
        """Run the whole transcribe_stream session in one worker-thread call.

        add_audio expects a 1-D float32 mx.array normalized to [-1, 1]; the final
        text is `tx.result.text` after the last chunk (there is no `.final()`).
        """
        import mlx.core as mx

        with model.transcribe_stream(context_size=_CONTEXT_SIZE) as tx:
            for chunk in chunks:
                tx.add_audio(mx.array(pcm16_to_float32(chunk)))
            return tx.result.text

    async def aclose(self) -> None:
        """Drop the model reference; MLX reclaims its buffers on GC."""
        self._model = None

    def _require_model(self) -> Any:
        if self._model is None:
            raise RuntimeError("parakeet model not loaded; call load() first")
        return self._model
