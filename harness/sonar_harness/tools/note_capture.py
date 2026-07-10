"""note.capture — save a quick note/idea/reminder into the Obsidian vault.

The frictionless-capture edge the voice loop was missing: "note that…", "jot this
down", "add to my ideas note". Writes are APPEND-ONLY and confined to Sonar's own
top-level ``Sonar/`` folder (the same folder todo_list/rag already treat as
Sonar-authored), so the tool can never clobber or corrupt the user's own notes.

Each capture becomes a bullet under a ``## YYYY-MM-DD`` day heading in the target
note (default ``Sonar/Inbox.md``); ``as_task`` writes it as a ``- [ ]`` checkbox so
it also shows up in ``todo_list``. The target name is slugified to a single safe
filename — no path separators, no ``..`` — so it always stays inside ``Sonar/``.

Note: the RAG index is a static startup snapshot (rag_backend.py), so a fresh
capture is in the vault immediately but only becomes semantically searchable after
the next index rebuild.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from sonar_harness.tools.base import ToolBase, ToolContext

_SONAR_DIR = "Sonar"
_DEFAULT_TARGET = "Inbox"
_MAX_SLUG_LEN = 60
_DAY_HEADING = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")
# Keep only filename-safe characters; everything else (incl. / and .) is dropped,
# which also defuses path traversal (no "..", no separators survive).
_UNSAFE = re.compile(r"[^A-Za-z0-9 _-]+")
_WS = re.compile(r"\s+")


def slug_for(target: str | None) -> str:
    """Reduce a target note name to one safe filename stem under Sonar/.

    Strips path separators / dots / other unsafe chars (so no ``..`` traversal),
    collapses whitespace, caps length. Empty/garbage falls back to the Inbox.
    """
    if not target or not target.strip():
        return _DEFAULT_TARGET
    cleaned = _WS.sub(" ", _UNSAFE.sub(" ", target)).strip()
    cleaned = cleaned[:_MAX_SLUG_LEN].strip()
    return cleaned or _DEFAULT_TARGET


def render_entry(text: str, as_task: bool, now: datetime) -> str:
    """One markdown bullet for a capture: a checkbox task or a timestamped note."""
    body = _WS.sub(" ", text.strip())
    if as_task:
        return f"- [ ] {body}"
    return f"- {now.strftime('%H:%M')} {body}"


def append_under_today(content: str, entry: str, today: str) -> str:
    """Append ``entry`` under a ``## <today>`` heading, adding the heading if the
    most recent day heading isn't today. Pure — returns the new file content."""
    last_day = None
    for line in content.splitlines():
        m = _DAY_HEADING.match(line)
        if m:
            last_day = m.group(1)
    base = content.rstrip()
    if last_day == today:
        return f"{base}\n{entry}\n"
    sep = "\n\n" if base else ""
    return f"{base}{sep}## {today}\n\n{entry}\n"


class NoteCaptureTool(ToolBase):
    name = "note.capture"
    description = (
        "Save a quick note, idea, thought, or reminder into the user's Obsidian "
        "vault (under their Sonar/ folder). Use when the user says things like "
        "'note that…', 'jot this down', 'remember this in my notes', 'capture…', "
        "or 'add to my <name> note'. Optional 'target' picks which note to append "
        "to (default is their Inbox); set 'as_task' true when it's a to-do so it "
        "becomes a checkbox. This WRITES to the vault. For a task YOU (the "
        "assistant) should track use todo_add; to READ existing notes use rag.search."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The note/idea/reminder to save, in the user's words.",
            },
            "target": {
                "type": "string",
                "description": (
                    "Optional note name to append to (e.g. 'ideas', 'groceries'). "
                    "Defaults to the Inbox. Created if it doesn't exist yet."
                ),
            },
            "as_task": {
                "type": "boolean",
                "description": "True if this is a to-do — writes it as a '- [ ]' checkbox.",
            },
        },
        "required": ["text"],
    }
    permission = "local"

    def __init__(self, *, vault_path: Path | str, now: datetime | None = None) -> None:
        self._vault = Path(vault_path)
        self._now = now  # None -> resolved per call (local time)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return "error: note.capture requires a non-empty 'text' string."
        if not self._vault.is_dir():
            return f"error: vault path {str(self._vault)!r} is not a directory."

        as_task = bool(args.get("as_task", False))
        target = args.get("target")
        slug = slug_for(target if isinstance(target, str) else None)

        now = self._now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        entry = render_entry(text, as_task, now)

        note_dir = self._vault / _SONAR_DIR
        note_path = note_dir / f"{slug}.md"
        try:
            note_dir.mkdir(parents=True, exist_ok=True)
            # Read the WHOLE current note, append to it, and write it back — the
            # existing content is always carried forward, never truncated. The
            # write is atomic (temp file + os.replace) so a crash mid-write can
            # never leave a note half-written or empty.
            existing = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
            if not existing.strip():
                existing = f"# {slug}\n"
            new_content = append_under_today(existing, entry, today)
            tmp_path = note_dir / f"{slug}.md.tmp"
            tmp_path.write_text(new_content, encoding="utf-8")
            os.replace(tmp_path, note_path)  # atomic swap on the same filesystem
        except OSError as exc:
            return f"error: could not save the note ({type(exc).__name__}: {exc})."

        rel = note_path.relative_to(self._vault).as_posix()
        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": "note.capture",
                "detail": f"appended to {rel}",
                "status": "ok",
            }
        )
        kind = "to-do" if as_task else "note"
        return f"Saved that {kind} to {rel}."
