"""daily.brief — the user's day at a glance, in ONE tool call.

"What's on my plate?" wants calendar + what's due + what landed in the inbox,
together. Rather than hope a small model chains calendar.agenda + todo_list +
gmail.search itself (and remembers all of them), this composes those existing
tools deterministically and hands back one grouped bundle for the model to
narrate.

**Read-back first.** The brief is PULL-only — nothing schedules it any more (it
used to be spoken aloud unprompted; see ``scheduler.py``). So the tool owns its
own artifact: the first ask of the day composes the brief and saves it to
``<vault>/Sonar/Brief/<date>.md``; every later ask that day reads that note
straight back, with no source-tool calls at all. ``refresh=true`` forces a
recompute. The note format matches the one ``scripts/morning_brief.py`` writes,
so either producer's artifact reads back cleanly.

Composition is pure: it instantiates the same read tools the registry uses and
calls their ``run`` with the shared ctx (so each still emits its own step-event),
then concatenates. Each sub-tool already degrades gracefully when a source isn't
connected, so the brief never crashes — a missing calendar just shows its "run
google-auth" hint in that section.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sonar_harness.tools.base import ToolBase, ToolContext
from sonar_harness.tools.calendar_read import CalendarAgendaTool
from sonar_harness.tools.gmail_read import GmailSearchTool
from sonar_harness.tools.todo_list import TodoListTool

log = logging.getLogger("sonar.tools.daily_brief")

# Recent, actually-important unread mail — kept tight so a brief stays skimmable.
_EMAIL_QUERY = "is:unread is:important newer_than:2d"
_EMAIL_MAX = 5

# `_generated 09:12 by Sonar_` — the freshness stamp both producers write.
_GENERATED_RE = re.compile(r"^_generated (\d{2}:\d{2}) by Sonar_\s*$", re.MULTILINE)


def _now() -> datetime:
    """Local now. Indirected so tests can pin the day without freezing the clock."""
    return datetime.now().astimezone()


def brief_path(vault_path: str | Path, day: date) -> Path:
    """The dated vault note for a day's brief."""
    return Path(vault_path) / "Sonar" / "Brief" / f"{day.isoformat()}.md"


def render_brief(sections: list[tuple[str, str]]) -> str:
    """Join labelled sections into one model-readable bundle (pure)."""
    return "\n\n".join(f"### {label}\n{content.strip()}" for label, content in sections)


def render_note(body: str, *, now: datetime) -> str:
    """The saved artifact: a dated heading, a freshness stamp, then the brief."""
    day = now.strftime("%Y-%m-%d")
    return (
        f"# Morning Brief — {day}\n\n"
        f"_generated {now.strftime('%H:%M')} by Sonar_\n\n"
        f"{body.strip()}\n"
    )


def parse_note(text: str) -> tuple[str, str | None]:
    """Split a saved brief into (body, generated_at). Tolerates a note written
    by either producer, or one hand-edited past recognition."""
    match = _GENERATED_RE.search(text)
    if match:
        return text[match.end():].strip(), match.group(1)
    # No stamp (hand-edited?): drop a leading markdown heading, keep the rest.
    body = re.sub(r"\A#[^\n]*\n+", "", text.strip())
    return body.strip(), None


class DailyBriefTool(ToolBase):
    name = "daily.brief"
    description = (
        "THE user's day at a glance, in ONE call: today's calendar, the to-dos "
        "that are overdue or due today, and recent unread important email. "
        "Use this for ANY question about what the user's day or workload looks "
        "like — 'what's on my plate', 'what's my day', 'my brief', 'the daily', "
        "'what's on today', 'what am I doing today', 'catch me up', 'give me the "
        "rundown', 'anything I'm missing'. Prefer it over calling calendar, "
        "to-do or email tools separately — it already includes all three, and "
        "re-asks the same day are served instantly from the saved brief. Only "
        "set 'refresh' true when the user explicitly wants it recomputed "
        "('refresh that', 'anything new since', 'check again'). Narrate the "
        "result warmly and briefly — lead with the calendar, then what's due, "
        "then email — and do NOT read ids, file paths or raw JSON aloud."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "refresh": {
                "type": "boolean",
                "description": (
                    "Recompute from live sources and overwrite today's saved "
                    "brief, instead of reading it back (default false)."
                ),
            },
        },
        "required": [],
    }
    permission = "local"

    def __init__(self, *, vault_path: str) -> None:
        self._vault = vault_path
        self._agenda = CalendarAgendaTool()
        self._todos = TodoListTool(vault_path=vault_path)
        self._gmail = GmailSearchTool()

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        now = _now()
        path = brief_path(self._vault, now.date())

        if not bool(args.get("refresh", False)):
            cached = self._read(path)
            if cached is not None:
                body, at = cached
                ctx.emit(
                    {
                        "step": "tool_result_summary",
                        "tool": self.name,
                        "detail": f"read back today's brief ({at or 'saved earlier'})",
                        "status": "ok",
                    }
                )
                when = f"at {at} today" if at else "earlier today"
                return (
                    f"(This brief was prepared {when} — relay it as-is. If the user "
                    f"wants it recomputed, call daily.brief again with refresh=true.)"
                    f"\n\n{body}"
                )

        body = render_brief(self._compose(ctx))
        saved = self._write(path, body, now=now)
        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": self.name,
                "detail": f"composed 4 sections{'' if saved else ' (not saved)'}",
                "status": "ok",
            }
        )
        return body

    # ---- sources -------------------------------------------------------------
    def _compose(self, ctx: ToolContext) -> list[tuple[str, str]]:
        """Every section, always — email included by design: the brief is the one
        place the user should have to look."""
        return [
            ("Today's calendar", self._agenda.run({"days": 1}, ctx)),
            ("Overdue to-dos", self._todos.run({"due": "overdue", "source": "user"}, ctx)),
            ("Due today", self._todos.run({"due": "today", "source": "user"}, ctx)),
            (
                "Unread important email",
                self._gmail.run({"query": _EMAIL_QUERY, "max_results": _EMAIL_MAX}, ctx),
            ),
        ]

    # ---- artifact ------------------------------------------------------------
    def _read(self, path: Path) -> tuple[str, str | None] | None:
        """Today's saved brief, or None if there isn't a usable one. A corrupt or
        unreadable note must never cost the user their brief — we just recompose."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("could not read saved brief %s (%s) — recomposing", path, exc)
            return None
        body, generated_at = parse_note(text)
        if not body:
            return None
        return body, generated_at

    def _write(self, path: Path, body: str, *, now: datetime) -> bool:
        """Save the brief atomically. Best-effort: a read-only vault costs the
        user the cache, not the answer."""
        tmp = path.parent / f"{path.name}.tmp"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(render_note(body, now=now), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError as exc:
            log.warning("could not save brief to %s (%s)", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
