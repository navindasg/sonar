"""Calendar WRITE tools — create, reschedule, and cancel events.

All three use the ``calendar.events`` OAuth scope and stay `local` so the
assistant can manage the calendar in a voice turn — the events are the user's
own and each action is reversible (a move can be moved back; a delete lands in
Google's Trash, recoverable ~30 days), unlike email send (gated/draft-only).
``reschedule``/``cancel`` take an ``event_id`` from ``calendar.agenda``; the model
is told to confirm with the user first. Not connected yet → each returns a
model-safe "run google-auth" string rather than crashing the turn.

The pure body/time builders are unit-tested; only ``run`` touches the network.
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


def _event_duration(event: dict[str, Any]) -> timedelta | None:
    """Length of an existing timed event, or None (all-day / unparseable)."""
    start = (event.get("start") or {}).get("dateTime")
    end = (event.get("end") or {}).get("dateTime")
    if not (isinstance(start, str) and isinstance(end, str)):
        return None
    try:
        return _parse_dt(end) - _parse_dt(start)
    except ValueError:
        return None


def rescheduled_times(
    event: dict[str, Any],
    new_start: datetime,
    args: dict[str, Any],
    *,
    default_duration_min: int = _DEFAULT_DURATION_MIN,
) -> tuple[datetime, datetime]:
    """New (start, end) for a move: explicit ``end`` wins, else ``duration_minutes``,
    else the event keeps its original length (else the 60-min default). Pure."""
    end_raw = args.get("end")
    if isinstance(end_raw, str) and end_raw.strip():
        return new_start, _parse_dt(end_raw)
    dur = args.get("duration_minutes")
    if dur is not None:
        try:
            return new_start, new_start + timedelta(minutes=max(1, int(dur)))
        except (TypeError, ValueError):
            pass
    original = _event_duration(event)
    if original is not None and original > timedelta(0):
        return new_start, new_start + original
    return new_start, new_start + timedelta(minutes=default_duration_min)


class CalendarRescheduleTool(ToolBase):
    name = "calendar.reschedule"
    description = (
        "Move an existing calendar event to a new time. Provide 'event_id' (from "
        "calendar.agenda) and a new 'start' ISO datetime (local time if no zone); "
        "the event keeps its original length unless you also give 'end' or "
        "'duration_minutes'. Use for 'move my 3pm to 4pm', 'push the meeting to "
        "tomorrow morning'. Confirm the change with the user first."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "The event's id, taken from a calendar.agenda result.",
            },
            "start": {
                "type": "string",
                "description": "New ISO-8601 start, e.g. '2026-07-10T16:00:00' (local if no offset).",
            },
            "end": {
                "type": "string",
                "description": "Optional new ISO-8601 end. Omit to keep the original length.",
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Optional new length in minutes (used only when 'end' is omitted).",
            },
        },
        "required": ["event_id", "start"],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        event_id = args.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            return "error: calendar.reschedule requires an 'event_id' from calendar.agenda."
        start_raw = args.get("start")
        if not isinstance(start_raw, str) or not start_raw.strip():
            return "error: calendar.reschedule requires a new 'start' ISO datetime."
        try:
            new_start = _parse_dt(start_raw)
        except ValueError as exc:
            return f"error: could not parse 'start' ({start_raw!r}): {exc}"

        try:
            service = build_service("calendar", "v3")
        except GoogleAuthError as exc:
            ctx.emit(_summary("calendar.reschedule", str(exc), status="error"))
            return str(exc)

        try:
            event = (
                service.events()
                .get(calendarId="primary", eventId=event_id.strip())
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 — likely a bad/stale id
            ctx.emit(_summary("calendar.reschedule", type(exc).__name__, status="error"))
            return (
                f"error: couldn't find that event ({type(exc).__name__}). "
                "Re-check the agenda for the right event id."
            )

        try:
            start, end = rescheduled_times(event, new_start, args)
        except ValueError as exc:
            return f"error: could not parse 'end': {exc}"

        body = {
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        try:
            updated = (
                service.events()
                .patch(calendarId="primary", eventId=event_id.strip(), body=body)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 — map API failure to model text
            ctx.emit(_summary("calendar.reschedule", type(exc).__name__, status="error"))
            return f"error: Calendar reschedule failed ({type(exc).__name__}): {exc}"

        title = str(updated.get("summary", "the event")).strip() or "the event"
        ctx.emit(_summary("calendar.reschedule", "moved"))
        return f"Moved '{title}' to {start.isoformat()}."


class CalendarCancelTool(ToolBase):
    name = "calendar.cancel"
    description = (
        "Cancel (delete) an existing calendar event. Provide 'event_id' (from "
        "calendar.agenda). Use for 'cancel my 3pm', 'delete the dentist "
        "appointment'. ALWAYS confirm with the user before cancelling — this "
        "removes the event from their calendar."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "The event's id, taken from a calendar.agenda result.",
            },
        },
        "required": ["event_id"],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        event_id = args.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            return "error: calendar.cancel requires an 'event_id' from calendar.agenda."

        try:
            service = build_service("calendar", "v3")
        except GoogleAuthError as exc:
            ctx.emit(_summary("calendar.cancel", str(exc), status="error"))
            return str(exc)

        try:
            service.events().delete(
                calendarId="primary", eventId=event_id.strip()
            ).execute()
        except Exception as exc:  # noqa: BLE001 — bad id or API failure
            ctx.emit(_summary("calendar.cancel", type(exc).__name__, status="error"))
            return f"error: Calendar cancel failed ({type(exc).__name__}): {exc}"

        ctx.emit(_summary("calendar.cancel", "cancelled"))
        return "Cancelled that event."


def _summary(tool: str, detail: str, *, status: str = "ok") -> dict[str, Any]:
    return {"step": "tool_result_summary", "tool": tool, "detail": detail, "status": status}
