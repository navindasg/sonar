"""todo_done — mark one of the assistant's OWN captured to-dos complete.

Operates on the harness ``todos`` table (the disposable list ``todo_add`` writes
and ``state_read(kind='todos')`` reads) — NOT the user's Obsidian checkboxes.
Target a todo by its ``id`` (as shown by ``state_read``); for a voice turn where
the id isn't handy, a ``text`` substring that matches exactly one open todo also
works. A completed todo is kept briefly (auto-expired by the state layer) then
swept, so "what's on your list" stops showing it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sonar_harness.tools.base import ToolBase, ToolContext


class TodoDoneTool(ToolBase):
    name = "todo_done"
    description = (
        "Mark one of YOUR captured to-dos (from state_read kind='todos') as "
        "done. Give its 'id', or a 'text' fragment matching exactly one open "
        "todo. CALL THIS whenever the user reports finishing a tracked task — "
        "'I finished X', 'I did X', 'done with X', 'completed X', 'mark X done', "
        "'cross off X'. This updates your own list, not the user's notes."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The todo's id (from state_read)."},
            "text": {
                "type": "string",
                "description": "Alternative to id: a fragment matching one open todo.",
            },
        },
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        ctx.state.expire_todos()  # don't complete something already stale
        row = self._resolve(args, ctx)
        if isinstance(row, str):
            return row  # an error / disambiguation message

        done_at = datetime.now(timezone.utc).isoformat()
        ctx.state.conn.execute(
            "UPDATE todos SET status = 'done', done_at = ? WHERE id = ?",
            (done_at, row["id"]),
        )
        ctx.state.conn.commit()
        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": "todo_done",
                "detail": f"done #{row['id']}",
                "status": "ok",
            }
        )
        return f"Marked done (#{row['id']}): {row['text']!r}."

    def _resolve(self, args: dict[str, Any], ctx: ToolContext):
        """Return the target row, or a str message (error / not-found / ambiguous)."""
        tid = args.get("id")
        if tid is not None:
            try:
                tid = int(tid)
            except (TypeError, ValueError):
                return "error: todo_done 'id' must be an integer."
            row = ctx.state.conn.execute(
                "SELECT id, text, status FROM todos WHERE id = ?", (tid,)
            ).fetchone()
            if row is None:
                return f"error: no todo with id {tid}."
            if row["status"] == "done":
                return f"Todo #{tid} ({row['text']!r}) is already done."
            return row

        text = args.get("text")
        if isinstance(text, str) and text.strip():
            matches = ctx.state.conn.execute(
                "SELECT id, text FROM todos WHERE status = 'open' "
                "AND lower(text) LIKE '%' || lower(?) || '%'",
                (text.strip(),),
            ).fetchall()
            if not matches:
                return f"error: no open todo matches {text.strip()!r}."
            if len(matches) > 1:
                listed = "; ".join(f"#{m['id']} {m['text']!r}" for m in matches)
                return f"error: {text.strip()!r} matches several todos — say which id: {listed}."
            return matches[0]

        return "error: todo_done needs an 'id' or a 'text' fragment."
