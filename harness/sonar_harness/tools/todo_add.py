"""todo_add — capture a task into the assistant's OWN to-do list (SQLite).

This is the harness's disposable working memory (brook37's ``todos`` table
pattern), NOT the user's Obsidian checkboxes — those are durable, live in the
vault, and are read via ``todo_list``. The two are deliberately separate: this
writes a row into the ``todos`` table (``state/schema.sql``); ``todo_list``
never looks here. Read these back with ``state_read(kind='todos')``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sonar_harness.tools.base import ToolBase, ToolContext

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class TodoAddTool(ToolBase):
    name = "todo_add"
    description = (
        "Capture a NEW follow-up task for YOU (the assistant) to track in your "
        "own list — use when the user asks you to remember to do something. "
        "This is your working memory, separate from the user's own notes; do "
        "NOT use it to read their existing to-dos (that's todo_list)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The task, phrased as a short imperative.",
            },
            "due": {
                "type": "string",
                "description": "Optional due date, ISO format YYYY-MM-DD.",
            },
        },
        "required": ["text"],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return "error: todo_add requires a non-empty 'text' string."
        text = text.strip()

        due = args.get("due")
        if due is not None:
            if not isinstance(due, str) or not _ISO_DATE.fullmatch(due.strip()):
                return "error: todo_add 'due' must be an ISO date (YYYY-MM-DD)."
            due = due.strip()

        ctx.state.expire_todos()  # keep the list tidy on every write
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            cur = ctx.state.conn.execute(
                "INSERT INTO todos (created_at, text, status, due) "
                "VALUES (?, ?, 'open', ?)",
                (created_at, text, due),
            )
            ctx.state.conn.commit()
        except Exception as exc:  # a DB failure shouldn't crash the turn
            return f"error: could not save the todo ({type(exc).__name__}: {exc})."

        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": "todo_add",
                "detail": f"saved todo #{cur.lastrowid}",
                "status": "ok",
            }
        )
        when = f" (due {due})" if due else ""
        return f"Saved to your list (#{cur.lastrowid}): {text!r}{when}."
