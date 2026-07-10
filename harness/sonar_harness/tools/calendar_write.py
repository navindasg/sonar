"""calendar.create — create an event on the user's Google Calendar.

A WRITE tool (via the ``calendar.events`` OAuth scope). Kept `local` so the
assistant can actually add events in a voice turn — a calendar event is the
user's own, low-consequence, and easily deleted (unlike email send, which stays
gated/draft-only). Not connected yet → returns a model-safe "run google-auth"
string rather than crashing the turn.

The event-body builder is pure and unit-tested; only ``run`` touches the network.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sonar_harness.google_auth import GoogleAuthError, build_service
from sonar_harness.tools.base import ToolBase, ToolContext

_DEFAULT_DURATION_MIN = 60


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 datetime; a naive one is taken as LOCAL time.

    A naive datetime is made timezone-aware in the machine's local zone, so its
    ``isoformat()`` carries an explicit UTC offset — which Google Calendar
    accepts without a separate ``timeZone`` field.
    """
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def build_event_body(
    args: dict[str, Any], *, default_duration_min: int = _DEFAULT_DURATION_MIN
) -> dict[str, Any]:
    """Build a Calendar ``events.insert`` body from model args (pure/testable).

    Raises ``ValueError`` (mapped to model text by ``run``) on missing/invalid
    summary or start. ``end`` defaults to start + ``duration_minutes`` (60).
    """
    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("calendar.create requires a non-empty 'summary'.")
    start_raw = args.get("start")
    if not isinstance(start_raw, str) or not start_raw.strip():
        raise ValueError(
            "calendar.create requires 'start' as an ISO datetime, e.g. "
            "'2026-07-10T15:00:00'."
        )
    try:
        start = _parse_dt(start_raw)
    except ValueError as exc:
        raise ValueError(f"could not parse 'start' ({start_raw!r}): {exc}") from exc

    end_raw = args.get("end")
    if isinstance(end_raw, str) and end_raw.strip():
        try:
            end = _parse_dt(end_raw)
        except ValueError as exc:
            raise ValueError(f"could not parse 'end' ({end_raw!r}): {exc}") from exc
    else:
        try:
            duration = int(args.get("duration_minutes", default_duration_min))
        except (TypeError, ValueError):
            duration = default_duration_min
        end = start + timedelta(minutes=max(1, duration))

    body: dict[str, Any] = {
        "summary": summary.strip(),
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    for key in ("description", "location"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            body[key] = val.strip()
    return body


class CalendarCreateTool(ToolBase):
    name = "calendar.create"
    description = (
        "Create an event on the user's Google Calendar. Provide 'summary' and a "
        "'start' ISO datetime (e.g. '2026-07-10T15:00:00', local time if no zone "
        "given); 'end' or 'duration_minutes' set the length (default 60 min). "
        "Optional 'description' and 'location'. Confirm the details with the user "
        "before creating. Cannot delete or move existing events."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Event title."},
            "start": {
                "type": "string",
                "description": "ISO-8601 start, e.g. '2026-07-10T15:00:00' (local if no offset).",
            },
            "end": {
                "type": "string",
                "description": "Optional ISO-8601 end. Omit to use duration_minutes.",
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Length in minutes when 'end' is omitted (default 60).",
            },
            "description": {"type": "string", "description": "Optional event notes."},
            "location": {"type": "string", "description": "Optional location."},
        },
        "required": ["summary", "start"],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            body = build_event_body(args)
        except ValueError as exc:
            return f"error: {exc}"

        try:
            service = build_service("calendar", "v3")
        except GoogleAuthError as exc:
            ctx.emit(_summary("calendar.create", str(exc), status="error"))
            return str(exc)

        try:
            event = service.events().insert(calendarId="primary", body=body).execute()
        except Exception as exc:  # noqa: BLE001 — map API failure to model text
            ctx.emit(_summary("calendar.create", type(exc).__name__, status="error"))
            return f"error: Calendar create failed ({type(exc).__name__}): {exc}"

        when = body["start"]["dateTime"]
        link = str(event.get("htmlLink", "")).strip()
        ctx.emit(_summary("calendar.create", "created"))
        return f"Created '{body['summary']}' at {when}. {link}".strip()


def _summary(tool: str, detail: str, *, status: str = "ok") -> dict[str, Any]:
    return {"step": "tool_result_summary", "tool": tool, "detail": detail, "status": status}
