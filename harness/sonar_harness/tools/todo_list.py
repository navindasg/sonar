"""todo_list — read OPEN task checkboxes ("- [ ]") straight from the vault.

The user's to-dos live in Obsidian as checkbox items scattered across notes;
neither the live-state DB (briefs / worker_runs) nor semantic ``rag.search``
surfaces them reliably — a semantic query for "my todos" doesn't gather
checkboxes that ARE the tasks. So this does a deterministic scan for OPEN
checkboxes and returns each with three things the model needs to reason about
them:

  * ``date``   — a due date parsed from a Tasks-plugin marker (``📅``/``⏳``) or,
                 failing that, the daily-note filename (``YYYY-MM-DD.md``). Lets
                 the model filter/sort by ``today`` / ``overdue`` / ``upcoming``
                 (it is told today's date via the prompt's <clock> block).
  * ``source`` — ``sonar`` if the note is one Sonar generated (under ``Sonar/``)
                 vs ``user`` for the user's own notes, so the assistant never
                 confuses what it wrote with what the user wrote.
  * ``note`` / ``line`` — provenance.

Read-only; the vault stays the single source of truth (no DB copy). Filtering is
done in the tool (args the model sets) so a 100+-item backlog isn't dumped into
context.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from sonar_harness.tools.base import ToolBase, ToolContext

# An OPEN Markdown/Obsidian checkbox: "- [ ]", "* [ ]", or "+ [ ]" at any indent
# (a space inside the brackets = not done). "[x]"/"[X]"/other states are skipped.
_OPEN_CHECKBOX = re.compile(r"^\s*[-*+]\s+\[ \]\s+(?P<text>\S.*?)\s*$")

# Obsidian Tasks-plugin dates. Prefer due (📅), then scheduled (⏳).
_DUE_DATE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
_SCHEDULED_DATE = re.compile(r"⏳\s*(\d{4}-\d{2}-\d{2})")
# A daily note is a file whose name is just a date, e.g. "2026-07-08.md".
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Never descend into Obsidian/VCS machinery or generated Sonar-runtime output.
_SKIP_DIRS = frozenset({".obsidian", ".trash", ".git", ".sonar"})

# Notes Sonar itself authors live under this top-level vault folder.
_SONAR_DIR = "Sonar"

_DUE_FILTERS = ("all", "today", "overdue", "upcoming", "dated", "undated")
_SOURCE_FILTERS = ("all", "user", "sonar")

# Cap so a spoken "rundown" stays reasonable; report when we truncate.
_MAX_TODOS = 100


def _task_date(text: str, note: str) -> str | None:
    """Due/scheduled date for a task line, or the daily-note date, else None."""
    m = _DUE_DATE.search(text) or _SCHEDULED_DATE.search(text)
    if m:
        return m.group(1)
    stem = note.rsplit("/", 1)[-1][:-3] if note.endswith(".md") else note
    return stem if _ISO_DATE.fullmatch(stem) else None


def _source_of(note: str) -> str:
    """'sonar' for notes Sonar generated (under Sonar/), else 'user'."""
    return "sonar" if note == _SONAR_DIR or note.startswith(_SONAR_DIR + "/") else "user"


def _scan_open_todos(vault: Path) -> list[dict[str, Any]]:
    """Walk ``vault`` for open checkboxes; annotate each with date + source."""
    todos: list[dict[str, Any]] = []
    for path in sorted(vault.rglob("*.md")):
        if any(part in _SKIP_DIRS for part in path.relative_to(vault).parts[:-1]):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue  # an unreadable note shouldn't abort the whole scan
        note = path.relative_to(vault).as_posix()
        for lineno, line in enumerate(content.splitlines(), start=1):
            match = _OPEN_CHECKBOX.match(line)
            if match:
                text = match.group("text")
                todos.append(
                    {
                        "task": text,
                        "note": note,
                        "line": lineno,
                        "date": _task_date(text, note),
                        "source": _source_of(note),
                    }
                )
    return todos


def _matches_due(todo_date: str | None, due: str, today: str) -> bool:
    if due in ("all",):
        return True
    if due == "undated":
        return todo_date is None
    if due == "dated":
        return todo_date is not None
    if todo_date is None:  # remaining filters are date comparisons
        return False
    if due == "today":
        return todo_date == today
    if due == "overdue":
        return todo_date < today
    if due == "upcoming":
        return todo_date > today
    return True


class TodoListTool(ToolBase):
    name = "todo_list"
    description = (
        "List open to-do checkboxes ('- [ ]') the USER wrote in their own "
        "Obsidian notes — their personal task list. Use for 'what are my todos', "
        "'my tasks', 'what's due today', 'anything overdue'. Do NOT use this for "
        "tasks the user asked YOU to remember — those you saved with todo_add and "
        "read back with state_read(kind='todos'). Filter with 'due' "
        "(today/overdue/upcoming — use the date from your <clock> context) and "
        "'source' (which vault note authored it). Summarize for the user."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "due": {
                "type": "string",
                "enum": list(_DUE_FILTERS),
                "description": (
                    "Date filter. today/overdue/upcoming compare a task's due "
                    "date (from '📅 YYYY-MM-DD' or its daily-note filename) to "
                    "today. 'undated' = no date. Default 'all'."
                ),
            },
            "source": {
                "type": "string",
                "enum": list(_SOURCE_FILTERS),
                "description": (
                    "'user' = the user's own notes; 'sonar' = notes Sonar "
                    "generated. Use 'user' for 'my todos'. Default 'all'."
                ),
            },
        },
    }
    permission = "local"

    def __init__(self, *, vault_path: Path | str, today: date | None = None) -> None:
        self._vault = Path(vault_path)
        self._today = today  # None -> resolved per call (local date)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if not self._vault.is_dir():
            return f"error: vault path {str(self._vault)!r} is not a directory."

        due = args.get("due") or "all"
        source = args.get("source") or "all"
        if due not in _DUE_FILTERS:
            return f"error: 'due' must be one of {list(_DUE_FILTERS)}."
        if source not in _SOURCE_FILTERS:
            return f"error: 'source' must be one of {list(_SOURCE_FILTERS)}."

        today = (self._today or date.today()).isoformat()
        try:
            todos = _scan_open_todos(self._vault)
        except OSError as exc:
            return f"error: could not read the vault ({type(exc).__name__}: {exc})."

        todos = [
            t
            for t in todos
            if _matches_due(t["date"], due, today)
            and (source == "all" or t["source"] == source)
        ]
        # Dated first (ascending), undated last; stable by note/line.
        todos.sort(key=lambda t: (t["date"] is None, t["date"] or "", t["note"], t["line"]))

        shown = todos[:_MAX_TODOS]
        detail = f"{len(todos)} open (due={due}, source={source})"
        if len(todos) > _MAX_TODOS:
            detail += f", showing {_MAX_TODOS}"
        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": "todo_list",
                "detail": detail,
                "status": "ok",
            }
        )
        if not todos:
            scope = "" if (due == "all" and source == "all") else f" for due={due}, source={source}"
            return f"No open to-do checkboxes found{scope}."
        return json.dumps(
            {
                "today": today,
                "filters": {"due": due, "source": source},
                "count": len(todos),
                "truncated": len(todos) > _MAX_TODOS,
                "todos": shown,
            },
            ensure_ascii=False,
        )
