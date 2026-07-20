"""daily.brief — the user's day at a glance, assembled in ONE tool call.

"What's my day look like?" wants calendar + what's due, together. Rather than hope
a small model chains calendar.agenda + todo_list itself (and remembers all of
them), this composes those existing tools deterministically and hands back one
grouped bundle for the model to narrate. Optionally pulls recent unread important
email too.

Pure composition: it instantiates the same read tools the registry uses and calls
their ``run`` with the shared ctx (so each still emits its own step-event), then
concatenates. Each sub-tool already degrades gracefully when a source isn't
connected, so the brief never crashes — a missing calendar just shows its
"run google-auth" hint in that section.
"""
from __future__ import annotations

from typing import Any

from sonar_harness.tools.base import ToolBase, ToolContext
from sonar_harness.tools.calendar_read import CalendarAgendaTool
from sonar_harness.tools.gmail_read import GmailSearchTool
from sonar_harness.tools.todo_list import TodoListTool

# Recent, actually-important unread mail — kept tight so a brief stays skimmable.
_EMAIL_QUERY = "is:unread is:important newer_than:2d"
_EMAIL_MAX = 5


def render_brief(sections: list[tuple[str, str]]) -> str:
    """Join labelled sections into one model-readable bundle (pure)."""
    return "\n\n".join(f"### {label}\n{content.strip()}" for label, content in sections)


class DailyBriefTool(ToolBase):
    name = "daily.brief"
    description = (
        "Assemble the user's day at a glance IN ONE CALL — today's calendar events "
        "plus the to-dos that are overdue or due today. Use for 'what's my day', "
        "'morning brief', 'what's on today', 'catch me up', 'give me the rundown'. "
        "Set 'include_email' true to also pull recent unread important email. "
        "Returns the items grouped by section; narrate them warmly and briefly — "
        "lead with the calendar, then what's due — and do NOT read ids or raw JSON "
        "aloud. Prefer this over calling calendar/todo tools separately for a brief."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "include_email": {
                "type": "boolean",
                "description": "Also include recent unread important email (default false).",
            },
        },
        "required": [],
    }
    permission = "local"

    def __init__(self, *, vault_path: str) -> None:
        self._agenda = CalendarAgendaTool()
        self._todos = TodoListTool(vault_path=vault_path)
        self._gmail = GmailSearchTool()

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        include_email = bool(args.get("include_email", False))
        sections: list[tuple[str, str]] = [
            ("Today's calendar", self._agenda.run({"days": 1}, ctx)),
            ("Overdue to-dos", self._todos.run({"due": "overdue", "source": "user"}, ctx)),
            ("Due today", self._todos.run({"due": "today", "source": "user"}, ctx)),
        ]
        if include_email:
            sections.append(
                (
                    "Unread important email",
                    self._gmail.run(
                        {"query": _EMAIL_QUERY, "max_results": _EMAIL_MAX}, ctx
                    ),
                )
            )
        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": "daily.brief",
                "detail": f"{len(sections)} sections",
                "status": "ok",
            }
        )
        return render_brief(sections)
