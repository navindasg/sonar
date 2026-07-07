"""todo_add — a STUB tool (ported shape from brook37 daemon/tools/todo_add.py).

brook37's version writes a row into a ``todos`` table. Sonar's live-state
schema has no todo table yet (it owns ``briefs`` + ``worker_runs``), so this
pass keeps the ToolBase surface real but the body a stub: it validates input
and acknowledges, emitting a step-event, without persisting. Swapping in a real
write is a body change once the worker stream adds the table.
"""

from __future__ import annotations

from typing import Any

from sonar_harness.tools.base import ToolBase, ToolContext


class TodoAddTool(ToolBase):
    name = "todo_add"
    description = (
        "Capture a follow-up task for the user (a reminder to act on later). "
        "Use when the user asks you to remember to do something."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The task, phrased as a short imperative.",
            }
        },
        "required": ["text"],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return "error: todo_add requires a non-empty 'text' string."
        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": "todo_add",
                "detail": "captured (stub — not persisted)",
                "status": "ok",
            }
        )
        # STUB: no todos table in state/schema.sql yet. Acknowledge only.
        return f"Noted (stub): {text.strip()!r}. (Not yet persisted — no todos table.)"
