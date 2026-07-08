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
import os
from typing import Any

import httpx

log = logging.getLogger("sonar.ollama")

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

# How long Ollama keeps a model resident after a call. The default ("5m") lets
# the hot model unload between turns, so the NEXT turn eats a multi-second cold
# reload (11 GB for e4b, ~18 GB for 26b). For an always-on ambient assistant we
# pin the model (-1 = forever) so warm turns stay ~1 s. Override via env with a
# whole number of seconds (e.g. "1800") or a Go duration string (e.g. "30m").
DEFAULT_KEEP_ALIVE = os.environ.get("SONAR_OLLAMA_KEEP_ALIVE", "-1")


def _coerce_keep_alive(value: str | int) -> str | int:
    """Normalize a keep_alive value to what Ollama's JSON parser accepts.

    Ollama wants either a NUMBER of seconds (-1 = forever, 0 = unload now) or a
    Go duration STRING ("30m", "1h"). The bare string "-1" is neither and 400s,
    so coerce anything integer-like to an int and pass duration strings through.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


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
        keep_alive: str = DEFAULT_KEEP_ALIVE,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self._base_url, timeout=timeout)
        self._keep_alive = _coerce_keep_alive(keep_alive)

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        num_predict: int = 512,
        num_ctx: int = 16384,
    ) -> dict[str, Any]:
        """Return the assistant ``message`` dict for one non-streaming turn.

        Raises RuntimeError on transport/HTTP failure with an actionable
        message (the caller maps it to a spoken apology + error step-event).

        ``num_predict`` HARD-caps output tokens: small models at temperature 0
        can slip into a repetition loop and, uncapped against the default
        131072 context, generate for MINUTES (observed: a chit-chat turn ran
        ~10 min). A spoken answer or a tool call is short, so 512 is generous
        headroom while bounding the worst case to seconds. ``num_ctx`` shrinks
        the KV cache from the wasteful 131072 default — plenty for the charter +
        tool schemas + retrieved passages (retrieval caps context at ~4k).
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": temperature,
                "num_predict": num_predict,
                "num_ctx": num_ctx,
            },
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

    def warm(self, model: str) -> bool:
        """Preload ``model`` into memory so the first real turn isn't a cold hit.

        Fires a 1-token generation with our ``keep_alive`` so the model loads
        and is pinned resident before any user turn. (The documented empty-
        ``messages`` load request 400s on this Ollama/MLX build, so we send a
        trivial real message instead — robust across versions.) Best-effort: a
        failure here just means the first turn pays the reload, so we log and
        move on rather than blocking startup.
        """
        try:
            resp = self._client.post(
                "/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "keep_alive": self._keep_alive,
                    "options": {"num_predict": 1},
                },
                timeout=120.0,
            )
            resp.raise_for_status()
            log.info("warmed model %s (keep_alive=%s)", model, self._keep_alive)
            return True
        except httpx.HTTPError as exc:
            log.warning("could not pre-warm model %s: %s", model, exc)
            return False

    def close(self) -> None:
        self._client.close()
