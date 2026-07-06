"""OpenAI-compatible LLM adapter (async HTTP SSE, no MLX offload).

Talks to any server exposing the OpenAI `/v1/chat/completions` Server-Sent
Events streaming API (LM Studio, vLLM, llama.cpp's server, OpenAI itself, ...).
Because it is plain async HTTP, it never touches MLX and therefore must NOT go
through `runtime.offload`/`stream_sync`: those serialize on the single-eval MLX
limiter, which would needlessly bottleneck a network call.

`httpx` is imported lazily inside `load()` so that importing this module — and
the registry/resolver that walk the providers package — stays cheap and works
without the HTTP stack installed.
"""
from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:  # types only — no runtime import of the heavy HTTP stack
    import httpx

logger = logging.getLogger("osvoice.llm.openai")

# Spec format: "http://host:port/v1#model". The fragment after "#" names the
# model; without it we fall back to this id (OpenAI's own server ignores it,
# local servers usually serve a single loaded model regardless).
_SPEC_SEP = "#"
_DEFAULT_MODEL = "default"
_SSE_DATA_PREFIX = "data: "
_SSE_DONE = "[DONE]"


def _parse_spec(spec: str) -> tuple[str, str]:
    """Split "base_url#model" into (base_url, model), trimming a trailing slash.

    A missing or empty "#model" fragment yields `_DEFAULT_MODEL` rather than an
    error: many local OpenAI-compatible servers serve one model by name-agnostic
    convention.
    """
    base, sep, model = spec.partition(_SPEC_SEP)
    base_url = base.strip().rstrip("/")
    if not base_url:
        raise ValueError(f"OpenAI LLM spec has no base URL: {spec!r}")
    model = model.strip() if sep else ""
    if not model:
        logger.warning(
            "OpenAI LLM spec %r has no '#model'; using %r", spec, _DEFAULT_MODEL
        )
        model = _DEFAULT_MODEL
    return base_url, model


class OpenAICompatLLM:
    """LLM slot backed by an OpenAI-compatible streaming chat endpoint."""

    def __init__(self, spec: str) -> None:
        self._base_url, self._model = _parse_spec(spec)
        self._client: httpx.AsyncClient | None = None

    async def load(self) -> None:
        """Open the shared async HTTP client. No weights to load (remote model)."""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "httpx is required for the OpenAI-compatible LLM adapter"
            ) from exc
        # Optional bearer auth: required by OpenAI/hosted endpoints, ignored by
        # keyless local servers (LM Studio, vLLM). Never hardcode the key.
        api_key = os.environ.get("OPENAI_API_KEY")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        try:
            self._client = httpx.AsyncClient(base_url=self._base_url, headers=headers)
        except Exception as exc:
            logger.exception("Failed to create HTTP client for %s", self._base_url)
            raise RuntimeError(
                f"Could not initialize OpenAI LLM client for {self._base_url!r}: {exc}"
            ) from exc

    async def aclose(self) -> None:
        """Close the underlying HTTP client and its connection pool."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yield assistant token deltas for an OpenAI-style `messages` list."""
        if self._client is None:
            raise RuntimeError("OpenAICompatLLM.stream called before load()")
        payload = {"model": self._model, "messages": messages, "stream": True}
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=payload
            ) as response:
                response.raise_for_status()
                async for delta in self._iter_deltas(response):
                    yield delta
        except Exception as exc:
            logger.exception("OpenAI LLM stream failed for model %s", self._model)
            raise RuntimeError(
                f"OpenAI LLM request to {self._base_url!r} failed: {exc}"
            ) from exc

    @staticmethod
    async def _iter_deltas(response: httpx.Response) -> AsyncIterator[str]:
        """Parse an SSE chat-completions stream into non-empty content deltas."""
        async for line in response.aiter_lines():
            if not line.startswith(_SSE_DATA_PREFIX):
                continue
            data = line[len(_SSE_DATA_PREFIX):].strip()
            if data == _SSE_DONE:
                return
            delta = _extract_delta(data)
            if delta:
                yield delta


def _extract_delta(data: str) -> str | None:
    """Pull `choices[0].delta.content` from one SSE JSON chunk, or None if absent."""
    try:
        obj = json.loads(data)
        choices = obj.get("choices") or []
        if not choices:
            return None
        return choices[0].get("delta", {}).get("content")
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError) as exc:
        logger.debug("Skipping malformed SSE chunk: %s (%s)", data, exc)
        return None
