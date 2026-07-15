"""AI overview for a finished notes session (local Ollama, structured JSON).

One direct /api/chat call — NOT a harness tool-loop turn: summarization needs
no tools, and the constrained ``format`` schema forces valid JSON out of the
model, which we render to markdown DETERMINISTICALLY (render_overview). So the
LLM only ever chooses content, never formatting, and a parse failure degrades
to the raw model text instead of losing the session.

Pure pieces (prompt build, parse, render) unit-test without IO; ``summarize``
does the single HTTP call via an injected httpx client.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from notes.session import SessionState, display_name

log = logging.getLogger("sonar.notes.summarize")

DEFAULT_MODEL = "gemma4:12b-mlx"   # the harness's `reason` alias: pinned resident
_TIMEOUT_S = 180.0
_MAX_TRANSCRIPT_CHARS = 60_000     # ~15k tokens; gemma-12b context is comfortable

# Constrained decoding schema for Ollama's `format` parameter.
OVERVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "person": {"type": "string"},
                    "item": {"type": "string"},
                },
                "required": ["person", "item"],
            },
        },
        "decisions": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "action_items", "decisions", "open_questions"],
}

_SYSTEM = (
    "You summarize meeting transcripts. The transcript lines are prefixed with "
    "the speaker's name. Respond ONLY with JSON matching the requested schema.\n"
    "- summary: 3-6 short bullets covering what was discussed and concluded.\n"
    "- action_items: every commitment or task, each with the PERSON responsible "
    "(use the speaker names as given; use 'Unassigned' when nobody owns it).\n"
    "- decisions: decisions actually made (empty list if none).\n"
    "- open_questions: unresolved questions or follow-ups (empty list if none).\n"
    "Be specific and faithful to the transcript; never invent content."
)


def transcript_text(state: SessionState) -> str:
    """The diarized transcript as prompt text (oldest lines dropped if huge)."""
    lines = [
        f"{display_name(state, seg.speaker)}: {seg.text}" for seg in state.segments
    ]
    text = "\n".join(lines)
    if len(text) > _MAX_TRANSCRIPT_CHARS:
        text = text[-_MAX_TRANSCRIPT_CHARS:]
        text = text[text.index("\n") + 1:] if "\n" in text else text
    return text


def build_messages(state: SessionState) -> list[dict[str, str]]:
    """The chat payload for the overview call."""
    user = f"Meeting: {state.title}\n\nTranscript:\n{transcript_text(state)}"
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]


def _unwrap_json(raw: str) -> str:
    """Pull the JSON object out of a model reply.

    Local gemma often ignores the constrained-decoding `format` and wraps its
    JSON in a ```json … ``` code fence (or adds a line of prose), which made
    json.loads fail and the whole fenced blob leak into the note verbatim.
    Strip a leading/trailing fence, then narrow to the outermost {...} so a
    stray preamble/suffix can't break the parse.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def parse_overview(raw: str) -> dict[str, Any] | None:
    """Parse the model's JSON reply; None if it isn't the expected shape."""
    try:
        obj = json.loads(_unwrap_json(raw))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("summary"), list):
        return None
    return obj


def _str_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def render_overview(overview: dict[str, Any]) -> str:
    """Deterministic markdown for the AI Overview section (people grouped)."""
    parts: list[str] = ["### Summary", ""]
    parts += [f"- {b}" for b in _str_items(overview.get("summary"))] or ["- (empty)"]

    items = overview.get("action_items")
    parts += ["", "### Action Items", ""]
    grouped: dict[str, list[str]] = {}
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            # The model isn't consistent about the key name for the task text
            # ("item" per our schema, but it often emits "task" or "action"),
            # so accept the common synonyms rather than silently dropping it.
            task = str(it.get("item") or it.get("task") or it.get("action") or "").strip()
            if not task:
                continue
            person = str(it.get("person") or it.get("owner") or "").strip() or "Unassigned"
            grouped.setdefault(person, []).append(task)
    if grouped:
        for person, tasks in grouped.items():
            parts += [f"- **{person}**"] + [f"  - [ ] {t}" for t in tasks]
    else:
        parts.append("- (none)")

    for key, heading in (("decisions", "Decisions"), ("open_questions", "Open Questions")):
        entries = _str_items(overview.get(key))
        if entries:
            parts += ["", f"### {heading}", ""] + [f"- {e}" for e in entries]
    return "\n".join(parts)


async def summarize(
    client: Any, state: SessionState, model: str = DEFAULT_MODEL
) -> str:
    """One overview call -> markdown. Any failure returns a visible fallback
    (never raises): the transcript must reach the vault even if Ollama is down.
    """
    if not state.segments:
        return "_(nothing was said)_"
    payload = {
        "model": model,
        "messages": build_messages(state),
        "stream": False,
        "format": OVERVIEW_SCHEMA,
        "options": {"temperature": 0.2},
    }
    try:
        resp = await client.post("/api/chat", json=payload, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        raw = (resp.json().get("message") or {}).get("content", "")
    except Exception as exc:  # noqa: BLE001 — degrade, never lose the transcript
        log.warning("overview call failed: %s", exc)
        return f"_(AI overview unavailable: {exc})_"
    overview = parse_overview(raw)
    if overview is None:
        log.warning("overview reply was not valid JSON; using raw text")
        return raw.strip() or "_(AI overview unavailable)_"
    return render_overview(overview)
