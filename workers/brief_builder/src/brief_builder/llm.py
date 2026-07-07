"""The single LLM leaf call: summarize gathered inputs into a brief body.

This is the ONLY place the worker touches a model. It is a bounded,
non-looping leaf: one request, one response, a timeout, and a graceful
fallback if Ollama is unreachable or errors.
"""

from __future__ import annotations

import httpx

from .config import Config
from .gather import NoteInput


class LLMError(RuntimeError):
    """Raised when the LLM leaf call fails (network, timeout, bad response)."""


def build_prompt(window: str, notes: tuple[NoteInput, ...]) -> str:
    """Compose the deterministic prompt from gathered notes."""
    if not notes:
        return (
            "There are no recent notes to summarize. Write a single, friendly "
            "sentence acknowledging a quiet day with nothing new in the vault."
        )

    lines = [
        f"You are assembling a short {window} brief for a single user.",
        "Below are their most-recently-edited notes (title + first line).",
        "Write a concise brief (3-6 short bullet points, plain markdown, no "
        "headings) that surfaces what's active and what might need attention.",
        "Do not invent facts beyond the notes. Be terse and useful.",
        "",
        "NOTES:",
    ]
    for i, note in enumerate(notes, start=1):
        excerpt = note.excerpt or "(no preview)"
        lines.append(f"{i}. {note.title} — {excerpt}")
    lines.append("")
    lines.append("BRIEF:")
    return "\n".join(lines)


def summarize(config: Config, notes: tuple[NoteInput, ...]) -> str:
    """Make the one leaf call to Ollama and return the brief body markdown.

    Uses Ollama's native /api/generate (non-streaming) — the worker only needs
    the finished text, and non-streaming turns are more reliable (RESEARCH.md).

    Raises:
        LLMError: on timeout, connection failure, HTTP error, or empty output.
    """
    prompt = build_prompt(config.window, notes)
    url = f"{config.ollama_host}/api/generate"
    payload = {
        "model": config.model_fast,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3},
    }

    try:
        response = httpx.post(url, json=payload, timeout=config.llm_timeout_s)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise LLMError(
            f"LLM call timed out after {config.llm_timeout_s}s at {url}"
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM call failed at {url}: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError(f"LLM returned non-JSON response from {url}") from exc

    text = str(data.get("response", "")).strip()
    if not text:
        raise LLMError("LLM returned an empty response")
    return text
