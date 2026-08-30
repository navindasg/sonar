"""weekly.review — "how did my week go", in ONE tool call.

The zoomed-out sibling of ``daily.brief``. Where the brief answers *what's ahead
today*, this answers *what actually happened this week*: the meetings that took
place, the to-dos that got finished versus the ones that slipped, the notes taken,
and which days got a brief at all. Composed deterministically from sources the
small model would otherwise have to remember to chain (and wouldn't).

**PULL-only.** Nothing schedules this and nothing speaks it — it runs because the
user asked, in that moment. (See ``scheduler.py``: proactive speech is not a thing
Sonar does any more.)

**What "the week" means — pinned deliberately.** Monday 00:00 local through the
following Monday 00:00, exclusive: the ISO week. ``week="this"`` (default) covers
Monday *through now*, so a Wednesday ask reviews Wednesday-to-date rather than
pretending about days that haven't happened; ``week="last"`` covers the whole
previous Monday-to-Sunday, which is what "how did my week go" means when it's
asked on a Monday morning. The clock is injected (``now`` constructor arg) so the
boundary is testable rather than whatever ``datetime.now()`` felt like.

**Read-back first**, same contract as ``daily.brief`` one resolution up: the first
ask of a week composes the review and saves it to
``<vault>/Sonar/Review/<iso-week>.md``; later asks that week read that note
straight back with no source scans at all, and ``refresh=true`` forces a
recompute. The staleness window is a week rather than a day, so the read-back
preamble carries the full *date* it was prepared (not just the time) and points
the model at ``refresh`` — a Friday re-ask of a Tuesday review must be able to say
"this is as of Tuesday".

**The two to-do systems are kept apart.** The user's own durable checkboxes live
in the vault; the assistant's disposable captures live in the harness DB
(``todos``). The charter tells the model never to conflate them, so the review
labels each explicitly instead of merging the counts. Note the DB list is *lazily
expired* (see ``State.expire_todos``) — completed captures are swept ~24h after
capture, so that sub-section reflects a short tail, not the whole week. This tool
never calls ``expire_todos`` itself: a review is read-only and must not delete the
user's rows as a side effect of being asked a question.

Every section degrades on its own — a disconnected calendar or an unreadable note
costs that one section, never the review.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sonar_harness.google_auth import GoogleAuthError, build_service
from sonar_harness.tools.base import ToolBase, ToolContext
from sonar_harness.tools.calendar_read import render_events
from sonar_harness.tools.daily_brief import brief_path, parse_note, render_brief
from sonar_harness.tools.todo_list import TodoListTool

log = logging.getLogger("sonar.tools.weekly_review")

_WEEK_CHOICES = ("this", "last")

# A week of meetings; the API's own ceiling for one page is 50.
_MAX_EVENTS = 50
# Per-section cap so a busy week can't flood a spoken turn's context.
_MAX_ITEMS = 40

# A COMPLETED Markdown/Obsidian checkbox. Mirrors todo_list's open-checkbox regex
# ("- [ ]"); "[x]"/"[X]" is the done state.
_DONE_CHECKBOX = re.compile(r"^\s*[-*+]\s+\[[xX]\]\s+(?P<text>\S.*?)\s*$")
# Obsidian Tasks-plugin completion marker — the only *reliable* "when was this
# finished" signal a vault checkbox carries.
_COMPLETED_DATE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Every Tasks-plugin date marker, for stripping at render time (see task_label).
_TASK_MARKERS = re.compile(r"\s*[✅📅⏳]\s*\d{4}-\d{2}-\d{2}")

# Sonar Notes frontmatter (see voice/notes/store.py: "created: YYYY-MM-DD HH:MM").
_NOTE_CREATED = re.compile(r"^created:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)
_NOTE_TITLE = re.compile(r"^#\s+(?P<title>\S.*?)\s*$", re.MULTILINE)

# `_generated 2026-07-29 17:30 by Sonar_` — this tool's freshness stamp. It
# carries the DATE as well as the time because a weekly artifact is read back
# days later; daily.brief's time-only stamp would be ambiguous here.
_GENERATED_RE = re.compile(
    r"^_generated (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) by Sonar_\s*$", re.MULTILINE
)

# Never descend into Obsidian/VCS machinery or generated Sonar-runtime output.
_SKIP_DIRS = frozenset({".obsidian", ".trash", ".git", ".sonar"})
# Sonar's own generated artifacts quote other notes back, so scanning them would
# double-count the very items they were built from.
_SKIP_PREFIXES = ("Sonar/Brief/", "Sonar/Review/")

_NOTES_DIR = Path("Sonar") / "Notes"


def _now() -> datetime:
    """Local now, timezone-aware. Indirected so tests can pin the week."""
    return datetime.now().astimezone()


# ---- week boundary -----------------------------------------------------------
def week_bounds(day: date, *, weeks_back: int = 0) -> tuple[date, date]:
    """The ISO week containing ``day`` as ``[Monday, next Monday)``.

    End-exclusive on purpose: a half-open interval is the only framing where
    "is this in the week" needs no special case for the last day.
    """
    monday = day - timedelta(days=day.isoweekday() - 1) - timedelta(weeks=weeks_back)
    return monday, monday + timedelta(days=7)


def week_key(monday: date) -> str:
    """The artifact key for a week, e.g. ``2026-W31``.

    Uses the ISO *year*, not the calendar year: 2027-01-01 belongs to 2026-W53,
    and keying it "2027-W01" would collide with the real 2027-W01 a week later.
    """
    iso = monday.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def review_path(vault_path: str | Path, monday: date) -> Path:
    """The vault note for one week's review."""
    return Path(vault_path) / "Sonar" / "Review" / f"{week_key(monday)}.md"


def coverage_end(now: datetime, end: date) -> date:
    """The last day a review of ``[…, end)`` can honestly cover, asked at ``now``.

    A review of days that haven't happened would be a forecast, and that is
    ``daily.brief``'s job — so the current week stops at today.
    """
    return min(now.date(), end - timedelta(days=1))


def cache_is_final(generated_at: str | None, as_of: date) -> bool:
    """Whether a saved review already covers everything up to ``as_of``.

    Only consulted for a week that has ENDED. While a week is still running the
    read-back contract wins outright — the artifact is week-shaped, re-asking on
    Wednesday what you asked on Monday costs nothing by design, and the preamble
    dates itself. But the ISO-week note is a single cache key that both
    ``week="this"`` and next Monday's ``week="last"`` land on, so a note composed
    *mid-week* covers only part of the week it is filed under. Serving that back
    to "how did last week go" — the Monday question this tool exists for — would
    report three days as seven and silently lose Thursday and Friday.

    A note with no stamp was hand-edited past recognition; that is the user's own
    text and not ours to second-guess, so it stands.
    """
    if generated_at is None:
        return True
    return generated_at[:10] >= as_of.isoformat()


def _start_of(day: date) -> datetime:
    """Local midnight at the start of ``day``, timezone-aware."""
    return datetime.combine(day, time.min).astimezone()


# ---- meetings ----------------------------------------------------------------
def drop_declined(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Events that plausibly *happened*, as a new list (input untouched).

    A meeting the user declined, or one that was cancelled outright, is on the
    calendar but was not part of their week — counting it would overstate how
    busy they were. Only the user's OWN ``declined`` response drops an event;
    someone else declining is just an absent attendee.
    """
    return [e for e in events if not _was_skipped(e)]


def _was_skipped(event: dict[str, Any]) -> bool:
    if str(event.get("status", "")).lower() == "cancelled":
        return True
    return any(
        a.get("self") and str(a.get("responseStatus", "")).lower() == "declined"
        for a in event.get("attendees") or []
    )


def fetch_meetings(start: datetime, end: datetime) -> str:
    """Calendar events in ``[start, end)``, rendered — or a model-safe reason why not.

    ``calendar.agenda`` can only look *forward* from now, so a backward window
    needs its own query; the rendering and the auth/failure wording are reused
    from that tool so both read identically to the model.
    """
    try:
        service = build_service("calendar", "v3")
    except GoogleAuthError as exc:
        return str(exc)
    try:
        resp = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=_MAX_EVENTS,
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 — map API failure to model text
        return f"error: Calendar request failed ({type(exc).__name__}): {exc}"
    return render_events(drop_declined(resp.get("items", [])))


# ---- vault scans -------------------------------------------------------------
def _in_range(day: str | None, start: date, end: date) -> bool:
    """ISO-date string inside the half-open ``[start, end)`` window."""
    return day is not None and start.isoformat() <= day < end.isoformat()


def _scannable(path: Path, vault: Path) -> str | None:
    """The vault-relative note path, or None if this file is out of bounds."""
    rel = path.relative_to(vault)
    if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
        return None
    note = rel.as_posix()
    return None if note.startswith(_SKIP_PREFIXES) else note


def _completed_on(text: str, note: str) -> str | None:
    """When a done checkbox was finished: its ✅ marker, else the daily-note date.

    Deliberately NOT the 📅/⏳ due date todo_list parses — a task due Monday and
    ticked off Friday belongs to Friday.
    """
    match = _COMPLETED_DATE.search(text)
    if match:
        return match.group(1)
    stem = note.rsplit("/", 1)[-1].removesuffix(".md")
    return stem if _ISO_DATE.fullmatch(stem) else None


def scan_completed_checkboxes(
    vault: Path, start: date, end: date
) -> list[dict[str, Any]]:
    """The user's ``- [x]`` checkboxes finished inside ``[start, end)``.

    Undated done-checkboxes are skipped rather than guessed at: a review that
    claims you finished something this week when it was ticked off months ago is
    worse than one that stays quiet.
    """
    done: list[dict[str, Any]] = []
    for path in sorted(vault.rglob("*.md")):
        note = _scannable(path, vault)
        if note is None:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue  # one unreadable note must not abort the whole scan
        for lineno, line in enumerate(content.splitlines(), start=1):
            match = _DONE_CHECKBOX.match(line)
            if not match:
                continue
            text = match.group("text")
            day = _completed_on(text, note)
            if _in_range(day, start, end):
                done.append(
                    {
                        "task": text,
                        "note": note,
                        "line": lineno,
                        "date": day,
                        "source": "sonar" if note.startswith("Sonar/") else "user",
                    }
                )
    done.sort(key=lambda d: (d["date"], d["note"], d["line"]))
    return done


def scan_notes(vault: Path, start: date, end: date) -> list[dict[str, Any]]:
    """Sonar Notes sessions saved inside ``[start, end)``.

    Notes are named by title, not date (voice/notes/store.py), so the date comes
    from the ``created:`` frontmatter the notes store writes — falling back to
    the file's mtime for a note that was hand-made or hand-edited past it.
    """
    notes: list[dict[str, Any]] = []
    for path in sorted((vault / _NOTES_DIR).glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            match = _NOTE_CREATED.search(content)
            day = (
                match.group(1)
                if match
                else date.fromtimestamp(path.stat().st_mtime).isoformat()
            )
        except OSError:
            continue
        if not _in_range(day, start, end):
            continue
        title = _NOTE_TITLE.search(content)
        notes.append(
            {
                "title": (title.group("title") if title else path.stem),
                "note": path.relative_to(vault).as_posix(),
                "date": day,
            }
        )
    notes.sort(key=lambda n: (n["date"], n["title"]))
    return notes


def scan_briefs(vault: Path, start: date, end: date) -> list[dict[str, Any]]:
    """Which days in ``[start, end)`` actually got a daily brief.

    Only the day and its stamp — inlining seven full briefs would bury the review
    it is supposed to summarize.
    """
    briefs: list[dict[str, Any]] = []
    day = start
    while day < end:
        path = brief_path(vault, day)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = None  # missing or unreadable — either way, no brief to report
        if text:
            briefs.append({"date": day.isoformat(), "generated_at": parse_note(text)[1]})
        day += timedelta(days=1)
    return briefs


# ---- the assistant's own list (harness DB) -----------------------------------
def _db_todos(state: Any, sql: str, params: tuple[Any, ...]) -> list[Any] | None:
    """Run one read against the ``todos`` table; None when state is unavailable.

    Read-only by construction: no ``expire_todos`` call, so asking for a review
    never deletes a row.
    """
    if state is None:
        return None
    try:
        return state.conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001 — DB drift must not kill the turn
        log.warning("weekly.review could not read the todos table (%s)", exc)
        return None


def _utc(moment: datetime) -> str:
    """A local instant as the ISO-8601 UTC string the DB stores, for lexical compare."""
    return moment.astimezone(timezone.utc).isoformat()


def completion_day(stamp: str) -> str:
    """The LOCAL calendar day a ``done_at`` stamp belongs to.

    The DB stores UTC (``todo_done`` writes ``datetime.now(timezone.utc)``) but
    the week under review is local, so the first ten characters of the raw string
    are the wrong day for anyone west of Greenwich — and can print a date outside
    the very window the review's own header says it covers. An unparseable stamp
    is handed back as-is: DB drift should cost a pretty date, not the review.
    """
    try:
        return datetime.fromisoformat(stamp).astimezone().date().isoformat()
    except (TypeError, ValueError):
        return str(stamp)[:10]


# ---- rendering ---------------------------------------------------------------
def task_label(task: str) -> str:
    """A checkbox's text with its Tasks-plugin date markers stripped.

    Rendering-only: the scan keeps the raw line for provenance. Every bullet
    already carries the date as its own field, so leaving the marker on would
    print it twice — and this tool's output gets read aloud, where "ship the
    parser check-mark twenty twenty-six oh seven twenty-eight" is noise.

    A task made of nothing BUT markers keeps its raw text: a bullet with a blank
    where the task should be reads as a bug, not as an empty task.
    """
    return _TASK_MARKERS.sub("", task).strip() or task.strip()


def _bullets(lines: list[str], empty: str) -> str:
    """A capped bullet list, or an explicit 'nothing here' line."""
    if not lines:
        return empty
    shown = lines[:_MAX_ITEMS]
    tail = "" if len(lines) <= _MAX_ITEMS else f"\n…and {len(lines) - _MAX_ITEMS} more."
    return "\n".join(f"- {line}" for line in shown) + tail


def render_review_note(body: str, *, now: datetime, monday: date) -> str:
    """The saved artifact: the week heading, a dated freshness stamp, the review."""
    return (
        f"# Weekly Review — {week_key(monday)}\n\n"
        f"_generated {now.strftime('%Y-%m-%d %H:%M')} by Sonar_\n\n"
        f"{body.strip()}\n"
    )


def parse_review_note(text: str) -> tuple[str, str | None]:
    """Split a saved review into ``(body, generated_at)``.

    Tolerates a note hand-edited past recognition: without a stamp we drop a
    leading heading and hand back whatever the user left there.
    """
    match = _GENERATED_RE.search(text)
    if match:
        return text[match.end():].strip(), match.group(1)
    body = re.sub(r"\A#[^\n]*\n+", "", text.strip())
    return body.strip(), None


class WeeklyReviewTool(ToolBase):
    name = "weekly.review"
    description = (
        "THE user's week in review, in ONE call: the meetings that actually "
        "happened, what they finished versus what's still open, the notes they "
        "took, and which days got a brief. Use for ANY look-back question about "
        "the week — 'how did my week go', 'what did I get done this week', 'my "
        "week in review', 'recap my week', 'how was last week', 'what happened "
        "this week'. Prefer it over calling calendar, to-do or note tools "
        "separately, and do NOT use it for what's ahead — that's daily.brief. "
        "Set 'week' to 'last' when the user means the previous week (and on a "
        "Monday, when 'my week' almost always means the one that just ended). "
        "Re-asks in the same week are served instantly from the saved review; "
        "only set 'refresh' true when the user wants it recomputed ('check "
        "again', 'anything since then'). Narrate it warmly and briefly — lead "
        "with what they got done — and do NOT read ids, file paths or raw JSON "
        "aloud."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "week": {
                "type": "string",
                "enum": list(_WEEK_CHOICES),
                "description": (
                    "'this' = Monday through today (default); 'last' = the whole "
                    "previous Monday-to-Sunday week."
                ),
            },
            "refresh": {
                "type": "boolean",
                "description": (
                    "Recompute from live sources and overwrite the saved review, "
                    "instead of reading it back (default false)."
                ),
            },
        },
        "required": [],
    }
    permission = "local"

    def __init__(
        self, *, vault_path: str, now: Callable[[], datetime] | None = None
    ) -> None:
        self._vault = Path(vault_path)
        self._now = now or _now

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        week = args.get("week") or "this"
        if week not in _WEEK_CHOICES:
            return f"error: 'week' must be one of {list(_WEEK_CHOICES)}."

        now = self._now()
        monday, next_monday = week_bounds(
            now.date(), weeks_back=1 if week == "last" else 0
        )
        path = review_path(self._vault, monday)
        as_of = coverage_end(now, next_monday)
        week_over = now.date() >= next_monday

        if not bool(args.get("refresh", False)):
            cached = self._read(path)
            # A week still in progress reads back however stale it is; a finished
            # one only if the saved note actually reaches its last day (see
            # cache_is_final — the two `week` values share one cache key).
            if cached is not None and (not week_over or cache_is_final(cached[1], as_of)):
                return self._read_back(cached, ctx)

        body = render_brief(self._compose(ctx, now=now, monday=monday, as_of=as_of))
        saved = self._write(path, body, now=now, monday=monday)
        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": self.name,
                "detail": f"reviewed {week_key(monday)}{'' if saved else ' (not saved)'}",
                "status": "ok",
            }
        )
        return body

    # ---- sections ------------------------------------------------------------
    def _compose(
        self, ctx: ToolContext, *, now: datetime, monday: date, as_of: date
    ) -> list[tuple[str, str]]:
        """Every section, always — an empty one is a signal ("no meetings all
        week"), not noise to be hidden.

        ``as_of`` is the last day the review covers (see ``coverage_end``); it is
        decided in ``run`` because the cache rule needs the same answer.
        """
        scan_end = as_of + timedelta(days=1)
        coverage = "Monday through today" if as_of >= now.date() else "the full week"
        return [
            (
                "Week covered",
                f"{monday.isoformat()} → {as_of.isoformat()} "
                f"({week_key(monday)}, {coverage})",
            ),
            ("Meetings", fetch_meetings(_start_of(monday), min(now, _start_of(scan_end)))),
            ("To-dos completed", self._completed(monday, scan_end, ctx)),
            ("To-dos still open", self._still_open(as_of, ctx)),
            ("Notes taken", self._notes(monday, scan_end)),
            ("Daily briefs", self._briefs(monday, scan_end)),
        ]

    def _completed(self, start: date, end: date, ctx: ToolContext) -> str:
        """Both to-do systems, labelled — the vault checkboxes and Sonar's own list."""
        try:
            ticked = scan_completed_checkboxes(self._vault, start, end)
        except OSError as exc:
            ticked = []
            log.warning("weekly.review could not scan the vault (%s)", exc)
        vault_lines = [
            f"{d['date']} — {task_label(d['task'])} ({d['note']})" for d in ticked
        ]

        rows = _db_todos(
            ctx.state,
            "SELECT text, done_at FROM todos WHERE status = 'done' "
            "AND done_at >= ? AND done_at < ? ORDER BY done_at",
            (_utc(_start_of(start)), _utc(_start_of(end))),
        )
        mine = (
            "(the assistant's own list is unavailable)"
            if rows is None
            else _bullets(
                [f"{completion_day(r['done_at'])} — {r['text']}" for r in rows],
                "Nothing you asked Sonar to track was completed in this window.",
            )
        )
        return (
            f"In your notes:\n{_bullets(vault_lines, 'No checkboxes ticked off this week.')}"
            f"\n\nOn the list you asked Sonar to keep:\n{mine}"
        )

    def _still_open(self, as_of: date, ctx: ToolContext) -> str:
        """What slipped: the user's overdue checkboxes plus Sonar's open captures.

        Overdue rather than every open task — the whole backlog is a different
        question ("what are my todos", todo_list), and dumping it would drown the
        one signal a review is for.
        """
        # Built per call, not in __init__: "overdue" is relative to the end of the
        # week under review, which the constructor cannot know.
        todos = TodoListTool(vault_path=self._vault, today=as_of)
        overdue = todos.run({"due": "overdue", "source": "user"}, ctx)

        rows = _db_todos(
            ctx.state,
            "SELECT text, due FROM todos WHERE status = 'open' "
            "ORDER BY (due IS NULL), due, created_at LIMIT ?",
            (_MAX_ITEMS,),
        )
        mine = (
            "(the assistant's own list is unavailable)"
            if rows is None
            else _bullets(
                [f"{r['text']}" + (f" (due {r['due']})" if r["due"] else "") for r in rows],
                "Nothing outstanding on the list you asked Sonar to keep.",
            )
        )
        return f"Overdue in your notes:\n{overdue}\n\nStill on Sonar's list:\n{mine}"

    def _notes(self, start: date, end: date) -> str:
        try:
            notes = scan_notes(self._vault, start, end)
        except OSError as exc:
            log.warning("weekly.review could not scan Sonar/Notes (%s)", exc)
            return "(could not read the notes folder)"
        return _bullets(
            [f"{n['date']} — {n['title']}" for n in notes],
            "No notes sessions were saved this week.",
        )

    def _briefs(self, start: date, end: date) -> str:
        briefs = scan_briefs(self._vault, start, end)
        return _bullets(
            [f"{b['date']}" + (f" (at {b['generated_at']})" if b["generated_at"] else "")
             for b in briefs],
            "No daily brief was generated this week.",
        )

    # ---- artifact ------------------------------------------------------------
    def _read_back(self, cached: tuple[str, str | None], ctx: ToolContext) -> str:
        body, at = cached
        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": self.name,
                "detail": f"read back the saved review ({at or 'saved earlier'})",
                "status": "ok",
            }
        )
        when = f"on {at}" if at else "earlier this week"
        return (
            f"(This review was prepared {when} — relay it as-is, and say when it's "
            f"from if that's not today. If the user wants it recomputed, call "
            f"weekly.review again with refresh=true.)\n\n{body}"
        )

    def _read(self, path: Path) -> tuple[str, str | None] | None:
        """This week's saved review, or None if there isn't a usable one. A corrupt
        note must never cost the user their review — we just recompose."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("could not read saved review %s (%s) — recomposing", path, exc)
            return None
        body, generated_at = parse_review_note(text)
        if not body:
            return None
        return body, generated_at

    def _write(self, path: Path, body: str, *, now: datetime, monday: date) -> bool:
        """Save the review atomically. Best-effort: a read-only vault costs the
        user the cache, not the answer."""
        tmp = path.parent / f"{path.name}.tmp"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(render_review_note(body, now=now, monday=monday), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError as exc:
            log.warning("could not save review to %s (%s)", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
