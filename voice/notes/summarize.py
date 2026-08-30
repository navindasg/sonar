"""AI overview for a finished notes session (local Ollama, structured JSON).

One direct /api/chat call — NOT a harness tool-loop turn: summarization needs
no tools, and the constrained ``format`` schema forces valid JSON out of the
model, which we render to markdown DETERMINISTICALLY (render_overview). So the
LLM only ever chooses content, never formatting, and a parse failure degrades
to the raw model text instead of losing the session.

Pure pieces (prompt build, parse, render) unit-test without IO; ``summarize``
does the single HTTP call via an injected httpx client.

The tail of this module reads the rendered overview back OUT of markdown
(``parse_action_items`` / ``user_action_items``), which is what lets store.py
turn the user's own commitments into real to-do checkboxes. It parses the
markdown rather than the overview dict on purpose: the dict is thrown away
after rendering, and the user can hand-edit the overview in the review UI —
so the markdown, not the model's JSON, is the source of truth by save time.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Iterable, Sequence

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
            # PLAIN bullets, deliberately not "- [ ]". The harness's todo_list
            # scans the whole vault for open checkboxes at any indent, so a
            # checkbox here would enrol every ATTENDEE's commitment in the
            # user's to-do list, and would list the user's own item twice (once
            # here, once dated in store.py's "## My Action Items"). Exactly one
            # section of a note is machine-scannable, and it is that one.
            # Ticking an item off in the review UI still works: type "[x]" and
            # parse_action_items drops it (the checkbox is optional there).
            parts += [f"- **{person}**"] + [f"  - {t}" for t in tasks]
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


# --- reading the overview back: whose action items are whose ------------------

# Sonar has no way to tell WHICH voice in the room is its user — diarization
# produces anonymous "S1"/"S2" clusters and the display names are free text the
# user types per session. Guessing (e.g. "the first speaker is you") would file
# other people's commitments as the user's to-dos, so identity is explicit
# config: SONAR_USER_NAME, optionally a comma-separated list of the labels the
# user gives themselves across sessions ("Navin, Navin Dasgupta, Me").
USER_NAME_ENV = "SONAR_USER_NAME"

_WS = re.compile(r"\s+")
# Any markdown heading — used both to find the Action Items block and to know
# where it ends (the next heading of any level).
_HEADING = re.compile(r"^ {0,3}#{1,6}\s")
_ACTION_HEADING = re.compile(r"^ {0,3}#{1,6}\s+Action Items\s*$", re.IGNORECASE)
# A person row: the whole line is just a bold name ("- **Navin**"). Anchored so
# prose that merely contains bold text can never be read as an owner.
_PERSON_LINE = re.compile(r"^(?P<indent>\s*)[-*+]\s+\*\*(?P<person>.+?)\*\*\s*:?\s*$")
# A task row NESTED under that person. The checkbox is optional (hand-edited
# overviews often lose it); a ticked one is captured so it can be dropped.
_TASK_LINE = re.compile(
    r"^(?P<indent>\s*)[-*+]\s+(?:\[(?P<mark>.)\]\s+)?(?P<task>\S.*?)\s*$"
)


def _squeeze(text: str) -> str:
    """One line, single-spaced — task text becomes a markdown checkbox, so a
    stray newline in model output must not spawn a second bullet or a heading."""
    return _WS.sub(" ", text).strip()


def parse_action_items(summary_md: str) -> tuple[tuple[str, str], ...]:
    """The (person, task) pairs in an overview's Action Items section.

    Reads back what ``render_overview`` wrote, and tolerates the shapes a user
    is likely to hand-type in the review UI (``*``/``+`` bullets, deeper indents,
    a missing checkbox). Items already ticked (``[x]``) are skipped — a
    commitment the user marked done must never be resurrected as an open
    checkbox. A task must be nested UNDER a person to count: a bullet at the
    person's own level is unowned, and unowned work is nobody's to-do.
    """
    items: list[tuple[str, str]] = []
    person: str | None = None
    person_indent = 0
    in_section = False
    for line in (summary_md or "").splitlines():
        if _HEADING.match(line):
            in_section = bool(_ACTION_HEADING.match(line))
            person = None
            continue
        if not in_section:
            continue
        owner = _PERSON_LINE.match(line)
        if owner:
            person = _squeeze(owner.group("person"))
            person_indent = len(owner.group("indent"))
            continue
        task = _TASK_LINE.match(line)
        if person is None or task is None or len(task.group("indent")) <= person_indent:
            continue
        if (task.group("mark") or " ") != " ":
            continue  # already ticked off
        text = _squeeze(task.group("task"))
        if text:
            items.append((person, text))
    return tuple(items)


def configured_user_names(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """The labels the user answers to, from ``SONAR_USER_NAME`` (may be empty)."""
    raw = (env if env is not None else os.environ).get(USER_NAME_ENV, "")
    return tuple(name for name in (part.strip() for part in raw.split(",")) if name)


def user_action_items(summary_md: str, user_names: Sequence[str]) -> tuple[str, ...]:
    """The user's OWN open action items, in order, without repeats.

    Empty when no name is configured — see USER_NAME_ENV for why we never guess.
    Matching is exact on a case- and whitespace-insensitive comparison: looser
    matching (prefixes, first names) would silently claim a colleague's items.
    """
    wanted = _folded(user_names)
    if not wanted:
        return ()
    seen: set[str] = set()
    mine: list[str] = []
    for person, task in parse_action_items(summary_md):
        if _squeeze(person).casefold() not in wanted:
            continue
        key = task.casefold()
        if key not in seen:
            seen.add(key)
            mine.append(task)
    return tuple(mine)


def _folded(names: Iterable[str]) -> frozenset[str]:
    """Comparable form of a name list, blanks dropped."""
    return frozenset(f for f in (_squeeze(n).casefold() for n in names) if f)
