"""Minimal Ollama chat client for the tool loop.

Talks to a local Ollama server's native ``/api/chat`` endpoint. Tool-selection
turns run NON-streaming (DECISIONS.md: only the final answer streams to voice),
so this exposes a single blocking ``chat()`` that returns the assistant message
dict (which carries ``content``, optional ``thinking``, and native
``tool_calls``).

Registry schemas are ``{name, description, input_schema}``; Ollama wants the
OpenAI function shape, so ``to_ollama_tools`` adapts them at the seam.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("sonar.ollama")

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


def to_ollama_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt registry ``{name, description, input_schema}`` -> Ollama tool shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["input_schema"],
            },
        }
        for s in schemas
    ]


class OllamaChat:
    """Thin blocking wrapper over ``POST /api/chat`` (stream=false)."""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout: float = 180.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self._base_url, timeout=timeout)

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Return the assistant ``message`` dict for one non-streaming turn.

        Raises RuntimeError on transport/HTTP failure with an actionable
        message (the caller maps it to a spoken apology + error step-event).
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if tools:
            payload["tools"] = tools
        try:
            resp = self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.exception("Ollama /api/chat failed for model %s", model)
            raise RuntimeError(
                f"Ollama request to {self._base_url!r} failed: {exc}. "
                f"Is it running (ollama serve) and is {model!r} pulled?"
            ) from exc
        body = resp.json()
        message = body.get("message")
        if not isinstance(message, dict):
            raise RuntimeError(f"Ollama returned no message: {body!r}")
        return message

    def close(self) -> None:
        self._client.close()
