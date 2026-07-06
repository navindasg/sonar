"""Ollama LLM adapter — async streaming chat over the local Ollama daemon.

Ollama exposes an async HTTP/NDJSON API, so this adapter talks to it directly
with `ollama.AsyncClient` and does NOT use `runtime.offload`: there is no MLX
eval here, only network I/O, which the event loop already multiplexes. (The MLX
limiter exists to serialize on-device evals; routing network calls through it
would needlessly stall STT/TTS.)

`load()` pins the model resident (`keep_alive=-1`) via a tiny warmup chat so the
first real turn doesn't pay a cold-start penalty. The heavy `ollama` import is
deferred into the methods so the registry/resolver can import this class without
the package installed.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:  # import only for type checkers; never at runtime/module load
    from ollama import AsyncClient

logger = logging.getLogger("osvoice.llm.ollama")

# Default model tag (includes the ollama colon-qualified variant).
_DEFAULT_MODEL = "gemma4:e4b-mlx"

# Keep the model resident across turns instead of unloading after each request.
_KEEP_ALIVE = -1

# Tokens for the warmup probe — just enough to force a real forward pass.
_WARMUP_NUM_PREDICT = 1


class OllamaLLM:
    """`LLMProvider` backed by a local Ollama daemon (async, no MLX offload)."""

    def __init__(self, spec: str = "", host: str | None = None) -> None:
        """`spec` is the full ollama tag (e.g. "gemma4:e4b-mlx"); empty -> default."""
        self.model = spec or _DEFAULT_MODEL
        self._host = host
        self._client: AsyncClient | None = None

    async def load(self) -> None:
        """Instantiate the async client and warm the model into residency."""
        try:
            from ollama import AsyncClient
        except ImportError as exc:  # pragma: no cover - env without ollama
            raise RuntimeError(
                "The 'ollama' package is required for OllamaLLM; install it and "
                "ensure the Ollama daemon is running."
            ) from exc

        self._client = AsyncClient(host=self._host)
        await self._warmup()

    async def _warmup(self) -> None:
        """Pin the model resident with a tiny non-streaming chat.

        Tolerant by design: a momentarily busy daemon should not abort startup,
        so failures are logged and swallowed — the first real turn will retry.
        """
        client = self._require_client()
        try:
            await client.chat(
                model=self.model,
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
                keep_alive=_KEEP_ALIVE,
                options={"num_predict": _WARMUP_NUM_PREDICT},
            )
            logger.info("Ollama model '%s' warmed and resident.", self.model)
        except Exception as exc:  # noqa: BLE001 - warmup must never crash startup
            logger.warning(
                "Ollama warmup for '%s' failed (continuing): %s", self.model, exc
            )

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yield assistant token deltas for an OpenAI-style `messages` list."""
        client = self._require_client()
        try:
            response = await client.chat(
                model=self.model,
                messages=messages,
                stream=True,
                keep_alive=_KEEP_ALIVE,
            )
            async for part in response:
                delta = part["message"]["content"]
                if delta:
                    yield delta
        except Exception as exc:  # noqa: BLE001 - surface a clear, logged failure
            logger.error("Ollama chat stream for '%s' failed: %s", self.model, exc)
            raise RuntimeError(
                f"Ollama chat stream failed for model '{self.model}': {exc}"
            ) from exc

    async def aclose(self) -> None:
        """Drop the client reference (the daemon keeps the model per keep_alive)."""
        self._client = None

    def _require_client(self) -> AsyncClient:
        """Return the loaded client or raise if `load()` was never called."""
        if self._client is None:
            raise RuntimeError("OllamaLLM.load() must be called before use.")
        return self._client
