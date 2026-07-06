"""mlx_lm direct LLM adapter for the osvoice pipeline.

Wraps `mlx_lm.stream_generate` — a *blocking* token generator — behind the
process-wide MLX gate. Loading and warmup go through `runtime.offload`; the hot
streaming path is driven by `runtime.stream_sync`, so each token step is offloaded
under `MLX_LIMITER(1)` and never races another MLX eval. The chat template is
rendered off the hot path before streaming begins.

Heavy backends (mlx_lm) are imported lazily inside methods so that importing this
module — and the registry that walks it — works without mlx installed.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from osvoice import runtime

logger = logging.getLogger("osvoice.llm.mlx")

# Small mlx-community chat model: fast to load, fits comfortably on an M3 Max.
DEFAULT_REPO = "mlx-community/Llama-3.2-3B-Instruct-4bit"

_MAX_TOKENS = 512
_WARMUP_MESSAGES = [{"role": "user", "content": "Hi"}]


class MLXLM:
    """`LLMProvider` adapter backed by a local mlx_lm chat model."""

    def __init__(self, spec: str | None = None) -> None:
        """`spec` is an HF repo id or local path; falls back to DEFAULT_REPO."""
        self._repo = spec or DEFAULT_REPO
        self._model: object | None = None
        self._tokenizer: object | None = None

    async def load(self) -> None:
        """Load weights on the MLX thread and warm with a 1-token generate."""
        try:
            self._model, self._tokenizer = await runtime.offload(self._load_sync)
        except Exception as exc:  # noqa: BLE001 — surface a clear load failure
            logger.exception("Failed to load mlx_lm model %r", self._repo)
            raise RuntimeError(f"mlx_lm load failed for {self._repo!r}: {exc}") from exc

        try:
            await runtime.offload(self._warmup_sync)
        except Exception as exc:  # noqa: BLE001
            logger.exception("mlx_lm warmup failed for %r", self._repo)
            raise RuntimeError(f"mlx_lm warmup failed for {self._repo!r}: {exc}") from exc

        logger.info("Loaded mlx_lm model %r", self._repo)

    def _load_sync(self) -> tuple[object, object]:
        """Blocking weight load (runs on the MLX worker thread)."""
        from mlx_lm import load  # lazy heavy import

        return load(self._repo)

    def _warmup_sync(self) -> None:
        """Blocking 1-token generate to trigger Metal kernel compilation."""
        from mlx_lm import stream_generate  # lazy heavy import

        prompt = self._render_prompt(_WARMUP_MESSAGES)
        for _ in stream_generate(self._model, self._tokenizer, prompt, max_tokens=1):
            break

    def _render_prompt(self, messages: list[dict]) -> str:
        """Render the chat template to a prompt string — off the hot path.

        ``tokenize=False`` keeps the return a ``str`` (the wrapper otherwise
        tokenizes by default); ``stream_generate`` accepts a string prompt.
        """
        if self._tokenizer is None:
            raise RuntimeError("MLXLM.stream() called before load()")
        return self._tokenizer.apply_chat_template(  # type: ignore[attr-defined]
            messages, add_generation_prompt=True, tokenize=False
        )

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yield assistant token deltas for an OpenAI-style `messages` list."""
        if self._model is None or self._tokenizer is None:
            raise RuntimeError("MLXLM.stream() called before load()")

        prompt = self._render_prompt(messages)  # built off the hot path

        def make_iter() -> object:
            from mlx_lm import stream_generate  # lazy heavy import

            return stream_generate(
                self._model, self._tokenizer, prompt, max_tokens=_MAX_TOKENS
            )

        try:
            async for response in runtime.stream_sync(make_iter):  # type: ignore[arg-type]
                yield response.text
        except Exception as exc:  # noqa: BLE001 — never fail silently mid-stream
            logger.exception("mlx_lm generation failed for %r", self._repo)
            raise RuntimeError(f"mlx_lm generation failed: {exc}") from exc

    async def aclose(self) -> None:
        """Drop references to the model/tokenizer so weights can be freed."""
        self._model = None
        self._tokenizer = None
