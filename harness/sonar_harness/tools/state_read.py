"""state_read — read-only view over the live-state SQLite (briefs / worker_runs).

Reads the two tables the checked-in ``state/schema.sql`` defines. This is the
harness's window into what the worker stream produced (the latest daily brief,
recent worker outcomes) so a voice turn can answer "what's my brief?" or "did
the brief builder run?" without touching the vault. Read-only: it never writes.
"""

from __future__ import annotations

import json
from typing import Any

from sonar_harness.tools.base import ToolBase, ToolContext


class StateReadTool(ToolBase):
    name = "state_read"
    description = (
        "Read the harness's live state. kind='todos' returns the tasks the user "
        "asked YOU to remember (the ones you saved with todo_add) — use this "
        "whenever the user asks 'what did I ask you to remember', 'what's on your "
        "list', or about your reminders. kind='brief' = the latest daily brief; "
        "kind='worker_runs' = recent background job outcomes. NOTE: this reads "
        "YOUR list, not the user's own notes — for those use todo_list."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["brief", "worker_runs", "todos"],
                "description": (
                    "'brief' = latest assembled brief; 'worker_runs' = recent "
                    "job outcomes; 'todos' = open tasks you captured via todo_add."
                ),
            }
        },
        "required": ["kind"],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        kind = args.get("kind")
        if kind not in ("brief", "worker_runs", "todos"):
            return "error: state_read 'kind' must be 'brief', 'worker_runs', or 'todos'."
        try:
            if kind == "brief":
                row = ctx.state.conn.execute(
                    "SELECT title, body_md, created_at FROM briefs "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                payload: Any = (
                    None
                    if row is None
                    else {
                        "title": row["title"],
                        "body_md": row["body_md"],
                        "created_at": row["created_at"],
                    }
                )
            elif kind == "worker_runs":
                rows = ctx.state.conn.execute(
                    "SELECT worker, status, started_at, finished_at, detail "
                    "FROM worker_runs ORDER BY started_at DESC LIMIT 5"
                ).fetchall()
                payload = [dict(r) for r in rows]
            else:  # todos — the assistant's own open list, soonest-due first
                ctx.state.expire_todos()  # drop stale ones before showing
                rows = ctx.state.conn.execute(
                    "SELECT id, text, due, created_at FROM todos "
                    "WHERE status = 'open' "
                    "ORDER BY (due IS NULL), due, created_at LIMIT 50"
                ).fetchall()
                payload = [dict(r) for r in rows]
        except Exception as exc:  # defensive — DB shape drift shouldn't kill a turn
            return f"error: could not read state ({type(exc).__name__}: {exc})."

        detail = "empty" if not payload else kind
        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": "state_read",
                "detail": detail,
                "status": "ok",
            }
        )
        if not payload:
            return f"No {kind} rows yet."
        return json.dumps(payload, ensure_ascii=False)
