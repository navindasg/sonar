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
        "Read the harness's live state: the most recent daily 'brief' the "
        "assistant assembled, or recent background 'worker_runs'. Use for "
        "questions about today's brief or whether a background job ran."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["brief", "worker_runs"],
                "description": "'brief' = latest assembled brief; 'worker_runs' = recent job outcomes.",
            }
        },
        "required": ["kind"],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        kind = args.get("kind")
        if kind not in ("brief", "worker_runs"):
            return "error: state_read 'kind' must be 'brief' or 'worker_runs'."
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
            else:
                rows = ctx.state.conn.execute(
                    "SELECT worker, status, started_at, finished_at, detail "
                    "FROM worker_runs ORDER BY started_at DESC LIMIT 5"
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
