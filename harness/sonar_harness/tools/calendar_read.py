"""calendar.agenda — read-only view of the user's upcoming Google Calendar events.

Uses the same per-user read-only OAuth as gmail (``google_auth``). Returns the
events in a window starting now, so the assistant can answer "what's on today?"
or "what's next?" grounded in the real calendar. Read-only: it never creates,
edits, or deletes events.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sonar_harness.google_auth import GoogleAuthError, build_service
from sonar_harness.tools.base import ToolBase, ToolContext

_DEFAULT_DAYS = 1
_MAX_DAYS = 31
_DEFAULT_MAX = 20
_MAX_RESULTS = 50


def _event_start(event: dict[str, Any]) -> str:
    """Human-ish start label: dateTime for timed events, date for all-day."""
    start = event.get("start") or {}
    return str(start.get("dateTime") or start.get("date") or "").strip()


def render_events(events: list[dict[str, Any]]) -> str:
    """Render Calendar events into compact model-readable lines.

    Each line carries the event ``id`` (when present) so the model can pass it to
    ``calendar.reschedule`` / ``calendar.cancel`` — it summarizes for the user and
    does not read the id aloud.
    """
    if not events:
        return "No events in that window."
    lines: list[str] = []
    for i, ev in enumerate(events, 1):
        when = _event_start(ev) or "(no time)"
        summary = str(ev.get("summary", "(no title)")).strip()
        location = str(ev.get("location", "")).strip()
        tail = f" @ {location}" if location else ""
        eid = str(ev.get("id", "")).strip()
        idtag = f"  [id: {eid}]" if eid else ""
        lines.append(f"[{i}] {when} — {summary}{tail}{idtag}")
    return "\n".join(lines)


class CalendarAgendaTool(ToolBase):
    name = "calendar.agenda"
    description = (
        "Read the user's upcoming Google Calendar events (read-only), starting "
        "now. Use for 'what's on today', 'what's next', 'am I free this "
        "afternoon'. 'days' sets the look-ahead window (1 = rest of today). "
        "Cannot create, move, or delete events."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": f"Look-ahead window in days from now (1-{_MAX_DAYS}, default {_DEFAULT_DAYS}).",
            },
            "max_results": {
                "type": "integer",
                "description": f"Max events to return (1-{_MAX_RESULTS}, default {_DEFAULT_MAX}).",
            },
        },
        "required": [],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        days = _clamp(args.get("days", _DEFAULT_DAYS), 1, _MAX_DAYS, _DEFAULT_DAYS)
        max_results = _clamp(args.get("max_results", _DEFAULT_MAX), 1, _MAX_RESULTS, _DEFAULT_MAX)

        try:
            service = build_service("calendar", "v3")
        except GoogleAuthError as exc:
            ctx.emit(_summary("calendar.agenda", str(exc), status="error"))
            return str(exc)

        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days)).isoformat()
        try:
            resp = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=max_results,
                )
                .execute()
            )
            events = resp.get("items", [])
        except Exception as exc:  # noqa: BLE001 — map API failure to model text
            ctx.emit(_summary("calendar.agenda", type(exc).__name__, status="error"))
            return f"error: Calendar request failed ({type(exc).__name__}): {exc}"

        ctx.emit(_summary("calendar.agenda", f"{len(events)} events"))
        return render_events(events)


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _summary(tool: str, detail: str, *, status: str = "ok") -> dict[str, Any]:
    return {"step": "tool_result_summary", "tool": tool, "detail": detail, "status": status}
