"""nudges — the SILENT "what wants my attention right now" engine.

This is the safe replacement for proactive audio. On 2026-07-24 Sonar spoke into
a live meeting; the lesson was not "tune the trigger" but "never let the machine
choose the moment". So the whole surface is **PULL-only**: nothing here speaks,
listens, schedules, notifies, or touches the voice socket. It computes a ranked
list of things that want attention and hands it back to whoever *asked* — a menu
bar the user is already looking at. If nobody polls, nothing happens, ever.

Four boring, well-understood nudges (deliberately boring — a surface that cries
wolf gets ignored, and an ignored surface is worse than none):

  * **calendar** — the next meeting starting within N minutes.
  * **email**    — unread *important* mail sitting older than N hours. Unread is
                   the proxy for "you haven't replied": Sonar reads mail, it has
                   no way to know a reply was sent from the phone, and unread is
                   the one signal that is both cheap and never a false positive.
  * **todo**     — vault checkboxes overdue by more than N days.
  * **notes**    — a notes session whose transcript was never saved (the only
                   nudge about something *lossy*, hence always high severity).

Two properties make it safe to poll and safe to trust:

**Deterministic ranking.** ``sort_nudges`` is a total order — severity, then
source, then per-source rank, then id. No tie is left to dict iteration or to
which worker thread finished first, so the same facts always render the same
list and the user's eye learns where to look.

**Graceful absence.** Every source runs in its own daemon thread behind a shared
deadline (``collect``). A source that raises, stalls on a cold network, or is
simply not connected costs its own nudge and nothing else — never an exception,
never an error row in the user's menu bar. Absence is the failure mode.

Composition, not reimplementation: the sources call the *existing* read tools
(``calendar.agenda`` / ``gmail.search`` / ``todo_list``) and parse their rendered
output, exactly as ``daily_brief`` composes them. The parsers are pinned to those
renderers by tests, so a format drift fails loudly in CI instead of quietly
emptying the surface in production.

Every threshold is an env var with a sane default (see the ``ENV_*`` constants);
a bad value logs once and falls back rather than breaking the poll.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from sonar_harness.tools.base import ToolContext
from sonar_harness.tools.calendar_read import CalendarAgendaTool
from sonar_harness.tools.gmail_read import GmailSearchTool
from sonar_harness.tools.todo_list import TodoListTool

log = logging.getLogger("sonar.nudges")

# ---- severity + source ordering ---------------------------------------------
HIGH = "high"
MEDIUM = "medium"
LOW = "low"

_SEVERITY_ORDER: dict[str, int] = {HIGH: 0, MEDIUM: 1, LOW: 2}
# Time-boxed things the user cannot recover if missed come first; a stale to-do
# will still be there in an hour.
_SOURCE_ORDER: dict[str, int] = {"calendar": 0, "notes": 1, "email": 2, "todo": 3}
# Anything unrecognised sorts LAST rather than crashing the sort: a future source
# that forgets to register here degrades to "shown at the bottom".
_UNRANKED = 99

# ---- tunables (env -> default) ----------------------------------------------
ENV_MEETING_MINUTES = "SONAR_NUDGE_MEETING_MINUTES"
ENV_MEETING_URGENT_MINUTES = "SONAR_NUDGE_MEETING_URGENT_MINUTES"
ENV_EMAIL_HOURS = "SONAR_NUDGE_EMAIL_HOURS"
ENV_EMAIL_MAX = "SONAR_NUDGE_EMAIL_MAX"
ENV_TODO_DAYS = "SONAR_NUDGE_TODO_DAYS"
ENV_TODO_URGENT_DAYS = "SONAR_NUDGE_TODO_URGENT_DAYS"
ENV_TODO_MAX = "SONAR_NUDGE_TODO_MAX"
ENV_NOTES_MINUTES = "SONAR_NUDGE_NOTES_MINUTES"
ENV_NOTES_MAX_AGE_H = "SONAR_NUDGE_NOTES_MAX_AGE_H"
ENV_MAX = "SONAR_NUDGE_MAX"
ENV_TTL_S = "SONAR_NUDGE_TTL_S"
ENV_TIMEOUT_S = "SONAR_NUDGE_TIMEOUT_S"

DEFAULT_MEETING_MINUTES = 30
DEFAULT_MEETING_URGENT_MINUTES = 5
DEFAULT_EMAIL_HOURS = 3.0
DEFAULT_EMAIL_MAX = 10
DEFAULT_TODO_DAYS = 3
DEFAULT_TODO_URGENT_DAYS = 14
DEFAULT_TODO_MAX = 5
DEFAULT_NOTES_MINUTES = 20.0
DEFAULT_NOTES_MAX_AGE_H = 24.0
DEFAULT_MAX = 8
DEFAULT_TTL_S = 60.0
# A poll must feel instant. Google on a cold connection can take seconds; past
# this budget the caller gets what finished and the rest is simply absent.
DEFAULT_TIMEOUT_S = 2.5

# `is:important` is Gmail's own priority signal — Sonar does not invent one.
# `in:inbox` drops archived mail; `newer_than:14d` keeps the request cheap and
# stops an ancient unread pile from permanently occupying the surface.
EMAIL_QUERY = "is:unread is:important in:inbox newer_than:14d"

# ---- source contract ---------------------------------------------------------
NudgeFn = Callable[[datetime], Iterable["Nudge"]]
NudgeSource = tuple[str, NudgeFn]


def _env_num(name: str, default: Any, cast: Callable[[str], Any]) -> Any:
    """Read a numeric env override; a junk value logs once and falls back.

    A misconfigured threshold must never be able to break the poll — the worst
    it can do is leave the default in place.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw.strip())
    except (TypeError, ValueError):
        log.warning("ignoring invalid %s=%r; using %r", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_num(name, default, int))


def _env_float(name: str, default: float) -> float:
    return float(_env_num(name, default, float))


@dataclass(frozen=True, slots=True)
class Nudge:
    """One thing wanting attention. Frozen: nudges are values, never edited.

    ``id``       stable across polls for the SAME underlying thing, so a surface
                 can diff, animate, or remember a dismissal without the row
                 flickering into a "new" nudge on the next tick.
    ``rank``     ordering WITHIN a source, lowest first (minutes-until-meeting,
                 negated days-overdue): small = more urgent.
    ``at``       the timestamp the nudge is about (ISO, or a plain due date) —
                 display only; ranking never re-parses it.
    """

    id: str
    source: str
    severity: str
    rank: int
    line: str
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready row — exactly the fields the rendering surface reads."""
        return {
            "id": self.id,
            "source": self.source,
            "severity": self.severity,
            "rank": self.rank,
            "line": self.line,
            "at": self.at,
        }


def _sort_key(nudge: Nudge) -> tuple[int, int, int, str]:
    return (
        _SEVERITY_ORDER.get(nudge.severity, _UNRANKED),
        _SOURCE_ORDER.get(nudge.source, _UNRANKED),
        nudge.rank,
        nudge.id,
    )


def _comparable(now: datetime) -> datetime:
    """Give ``now`` a timezone so it can be compared with parsed timestamps.

    Everything the parsers produce is aware, so a naive clock used to raise
    TypeError inside a source. That is exactly the failure this module refuses
    to have: the source would be swallowed by ``collect`` and the surface would
    go quietly empty. Assume local time — the same assumption ``astimezone``
    makes everywhere else here — rather than lose the poll.
    """
    return now if now.tzinfo else now.astimezone()


def sort_nudges(nudges: Iterable[Nudge]) -> tuple[Nudge, ...]:
    """The deterministic order (pure; the caller's sequence is untouched).

    Ends in ``id`` so the order is TOTAL: two otherwise-identical nudges can
    never swap places between polls just because a thread finished first.
    """
    return tuple(sorted(nudges, key=_sort_key))


# ---- calendar ----------------------------------------------------------------
# `render_events`: "[1] <ISO-or-date> — <summary>[ @ loc][  [id: <eid>]]"
_AGENDA_LINE = re.compile(r"^\[\d+\]\s+(?P<when>\S+)\s+—\s+(?P<rest>.+?)\s*$")
_ID_TAG = re.compile(r"\s*\[id:\s*(?P<id>[^\]]*)\]\s*$")


def parse_agenda(text: str) -> tuple[tuple[datetime, str, str], ...]:
    """``calendar.agenda`` output -> ``(start, label, event_id)``, earliest first.

    Anything that is not an event line (the "not connected" hint, "No events in
    that window.", a blank body) simply yields nothing — the caller cannot tell
    an empty calendar from an absent one, and deliberately so.
    """
    parsed: list[tuple[datetime, str, str]] = []
    for line in (text or "").splitlines():
        match = _AGENDA_LINE.match(line)
        if not match:
            continue
        when = match.group("when")
        # All-day events are date-only: "starts in N minutes" is meaningless for
        # them, and reading one as local midnight would fire a bogus late-evening
        # nudge every single day.
        if "T" not in when:
            continue
        try:
            start = datetime.fromisoformat(when)
        except ValueError:
            continue
        rest = match.group("rest")
        tag = _ID_TAG.search(rest)
        parsed.append(
            (
                start if start.tzinfo else start.astimezone(),
                _ID_TAG.sub("", rest).strip(),
                tag.group("id").strip() if tag else "",
            )
        )
    return tuple(sorted(parsed, key=lambda item: item[0]))


def meeting_nudges(agenda_text: str, now: datetime) -> tuple[Nudge, ...]:
    """At most ONE nudge: the next meeting, if it starts inside the window.

    Only the next one — a menu bar showing three upcoming meetings is an agenda,
    not a nudge, and the user already has ``daily.brief`` for that.
    """
    window = _env_int(ENV_MEETING_MINUTES, DEFAULT_MEETING_MINUTES)
    urgent = _env_int(ENV_MEETING_URGENT_MINUTES, DEFAULT_MEETING_URGENT_MINUTES)
    now = _comparable(now)

    for start, label, event_id in parse_agenda(agenda_text):
        seconds = (start - now).total_seconds()
        if seconds < 0:
            continue  # already underway: nudging about it is just nagging
        minutes = int(seconds // 60)
        if minutes > window:
            return ()  # the earliest upcoming one is already too far out
        when = "now" if minutes <= 0 else f"in {minutes} min"
        return (
            Nudge(
                # The calendar's own id keeps the nudge identical across polls;
                # the start time is the fallback when a renderer drops the tag.
                id=f"meeting:{event_id or start.isoformat()}",
                source="calendar",
                severity=HIGH if minutes <= urgent else MEDIUM,
                rank=minutes,
                line=f"{label or 'Untitled event'} starts {when}",
                at=start.isoformat(),
            ),
        )
    return ()


# ---- email -------------------------------------------------------------------
# `render_messages`: "[1] <subject> — <from> (<RFC-2822 date>)" + an indented
# snippet line (which cannot match, since it does not start with "[n]").
#
# The date group tolerates ONE level of nested "(…)" because a real Date header
# almost always carries an RFC-2822 timezone comment — "… -0400 (EDT)". A group
# that stopped at the first ")" matched nothing on those lines, so most of a
# real inbox was silently dropped and the email nudge simply never fired.
# `parsedate_to_datetime` understands the comment itself, so it is passed through.
_MAIL_LINE = re.compile(
    r"^\[\d+\]\s+(?P<row>.+?)\s+\((?P<date>[^()]*(?:\([^()]*\)[^()]*)*)\)\s*$"
)

# render_messages joins the subject and the sender with this exact separator.
_MAIL_SEP = " — "


def _subject_of(row: str) -> str:
    """``"<subject> — <sender>"`` -> the subject.

    Split at the LAST separator whose tail still looks like an address, not the
    first: em dashes inside subjects are everywhere ("Q3 — final numbers") and
    splitting at the first one truncated them to a useless stub. Falls back to
    the whole row when there is no sender to find (the From header can be empty).
    """
    parts = row.split(_MAIL_SEP)
    for cut in range(len(parts) - 1, 0, -1):
        if "@" in _MAIL_SEP.join(parts[cut:]):
            return _MAIL_SEP.join(parts[:cut]).strip()
    return row.strip().rstrip("—").strip()


def parse_messages(text: str) -> tuple[tuple[str, datetime], ...]:
    """``gmail.search`` output -> ``(subject, received)``, in the order given.

    A message whose Date header will not parse is dropped rather than guessed
    at: an invented timestamp would age into a phantom nudge that never clears.
    """
    parsed: list[tuple[str, datetime]] = []
    for line in (text or "").splitlines():
        match = _MAIL_LINE.match(line)
        if not match:
            continue
        try:
            received = parsedate_to_datetime(match.group("date").strip())
        except (TypeError, ValueError):
            continue
        if received is None:  # older stdlib signalled failure with None
            continue
        parsed.append(
            (
                _subject_of(match.group("row")),
                received if received.tzinfo else received.astimezone(),
            )
        )
    return tuple(parsed)


def email_nudges(search_text: str, now: datetime) -> tuple[Nudge, ...]:
    """One aggregate nudge for the unread-important pile, or nothing.

    Aggregate on purpose: five rows for five emails would crowd out the meeting
    that actually starts in four minutes. Count + oldest subject is enough for
    the user to decide whether to open the inbox.
    """
    hours = _env_float(ENV_EMAIL_HOURS, DEFAULT_EMAIL_HOURS)
    now = _comparable(now)
    cutoff = now - timedelta(hours=hours)
    stale = sorted(
        (m for m in parse_messages(search_text) if m[1] <= cutoff),
        key=lambda m: m[1],
    )
    if not stale:
        return ()

    subject, oldest = stale[0]
    waited_h = int((now - oldest).total_seconds() // 3600)
    noun = "email" if len(stale) == 1 else "emails"
    return (
        Nudge(
            # Aggregate id: the pile is one thing, so a new arrival must not
            # look like a brand-new nudge to a surface tracking dismissals.
            id="email:unread-important",
            source="email",
            severity=MEDIUM,
            # Negated so a longer wait sorts first, matching the to-do rank.
            rank=-waited_h,
            line=(
                f"{len(stale)} unread important {noun} — oldest "
                f"“{subject or '(no subject)'}” ({waited_h}h)"
            ),
            at=oldest.isoformat(),
        ),
    )


# ---- to-dos ------------------------------------------------------------------
# Obsidian Tasks-plugin markers, stripped from the DISPLAY line only (the due
# date is already stated in words, and "📅 2026-07-20" is noise in a menu bar).
_TASK_MARKER = re.compile(r"\s*[📅⏳]\s*\d{4}-\d{2}-\d{2}")


def _todo_rows(list_text: str) -> tuple[dict[str, Any], ...]:
    """The ``todos`` array from ``todo_list``'s JSON, or nothing.

    ``todo_list`` answers in prose when it has nothing to say ("No open to-do
    checkboxes found…") or when the vault is unreadable ("error: …"), so a
    JSON-decode failure is an EXPECTED path, not an anomaly.
    """
    try:
        payload = json.loads(list_text)
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, dict):
        return ()
    rows = payload.get("todos")
    if not isinstance(rows, list):
        return ()
    return tuple(row for row in rows if isinstance(row, dict))


def _stable_id(*parts: str) -> str:
    """A short content hash. Not security: it just has to be stable + collision-
    free enough that two different tasks never share a row identity."""
    joined = "\x00".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _to_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _todo_nudge(days: int, row: dict[str, Any], urgent_days: int) -> Nudge:
    task = str(row.get("task", "")).strip()
    note = str(row.get("note", "")).strip()
    label = _TASK_MARKER.sub("", task).strip() or "(untitled task)"
    plural = "day" if days == 1 else "days"
    return Nudge(
        # Keyed on note + task text, NOT the line number: editing elsewhere in
        # the note shifts every line below it, and an id that moved with it
        # would make a month-old task look brand new on the next poll.
        id=f"todo:{_stable_id(note, task)}",
        source="todo",
        severity=HIGH if days >= urgent_days else MEDIUM,
        rank=-days,  # most overdue first
        line=f"“{label}” — {days} {plural} overdue",
        at=str(row.get("date") or ""),
    )


def todo_nudges(list_text: str, now: datetime) -> tuple[Nudge, ...]:
    """Vault checkboxes overdue by MORE than the threshold, most overdue first."""
    threshold = _env_int(ENV_TODO_DAYS, DEFAULT_TODO_DAYS)
    urgent_days = _env_int(ENV_TODO_URGENT_DAYS, DEFAULT_TODO_URGENT_DAYS)
    cap = max(0, _env_int(ENV_TODO_MAX, DEFAULT_TODO_MAX))
    today = now.date()

    overdue: list[tuple[int, dict[str, Any]]] = []
    for row in _todo_rows(list_text):
        due = _to_date(row.get("date"))
        if due is None:
            continue  # undated tasks are a backlog, not a deadline
        days = (today - due).days
        if days > threshold:
            overdue.append((days, row))

    # Sort before the cap so the cap keeps the WORST offenders, not the first
    # ones the vault scan happened to reach. Note/task break ties deterministically.
    overdue.sort(key=lambda item: (-item[0], str(item[1].get("note", "")), str(item[1].get("task", ""))))
    return tuple(_todo_nudge(days, row, urgent_days) for days, row in overdue[:cap])


# ---- unsaved notes session ---------------------------------------------------
def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except (OSError, ValueError, OverflowError):
        return None


def read_notes_marks(vault_path: Path | str) -> tuple[datetime | None, datetime | None]:
    """``(session_started, last_note_saved)`` — the two filesystem marks that say
    whether a notes session made it into the vault.

    ``~/.sonar/run/notes.url`` is written when the notes UI comes up and is never
    cleaned up (see ``app/Sources/SonarApp/NotesURLWatcher.swift``), so its mtime
    is a reliable "a session STARTED at" mark and an unreliable "one is running"
    one. Pairing it with the newest note under ``<vault>/Sonar/Notes`` is what
    turns it into an answer: a note saved after the session began means the
    transcript landed. Missing marks are ``None``, never an error.
    """
    home = Path(os.environ.get("SONAR_HOME", str(Path.home() / ".sonar")))
    started = _mtime(home / "run" / "notes.url")

    saved: datetime | None = None
    try:
        for note in (Path(vault_path) / "Sonar" / "Notes").glob("*.md"):
            stamp = _mtime(note)
            if stamp is not None and (saved is None or stamp > saved):
                saved = stamp
    except OSError as exc:  # unreadable vault costs this nudge, nothing else
        log.warning("could not scan saved notes (%s: %s)", type(exc).__name__, exc)
    return started, saved


def _aware(moment: datetime, now: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=now.tzinfo)


def notes_nudges(
    started: datetime | None, last_saved: datetime | None, now: datetime
) -> tuple[Nudge, ...]:
    """HIGH severity when a session's transcript looks like it was never saved.

    The only nudge about something LOSSY — a meeting you cannot re-record — which
    is why it outranks everything else at the same tier. Bounded on both sides:
    below the grace window the session is probably still running, and past the
    expiry the marker is more likely a discarded session than an unsaved one, so
    the nudge retires instead of nagging forever.
    """
    if started is None:
        return ()
    now = _comparable(now)
    started = _aware(started, now)
    if last_saved is not None and _aware(last_saved, now) >= started:
        return ()

    minutes_open = (now - started).total_seconds() / 60.0
    grace = _env_float(ENV_NOTES_MINUTES, DEFAULT_NOTES_MINUTES)
    max_age_min = _env_float(ENV_NOTES_MAX_AGE_H, DEFAULT_NOTES_MAX_AGE_H) * 60.0
    if minutes_open < grace or minutes_open > max_age_min:
        return ()

    return (
        Nudge(
            id=f"notes:{started.strftime('%Y%m%dT%H%M%S')}",
            source="notes",
            severity=HIGH,
            rank=0,  # there is only ever one session marker
            line=(
                f"Notes session from {started.strftime('%H:%M')} was never saved "
                f"({int(minutes_open)} min ago)"
            ),
            at=started.isoformat(),
        ),
    )


# ---- collection --------------------------------------------------------------
def _run_source(
    name: str,
    fn: NudgeFn,
    now: datetime,
    into: dict[str, tuple[Nudge, ...]],
    lock: threading.Lock,
) -> None:
    """Run one source; swallow ANY failure so it costs only its own nudge."""
    try:
        produced = tuple(n for n in (fn(now) or ()) if isinstance(n, Nudge))
    except Exception as exc:  # noqa: BLE001 — a polled surface never shows an error
        log.warning("nudge source %r failed (%s: %s)", name, type(exc).__name__, exc)
        return
    with lock:
        into[name] = produced


def collect(
    sources: Sequence[NudgeSource], now: datetime, *, timeout_s: float | None = None
) -> tuple[Nudge, ...]:
    """Run every source concurrently under ONE deadline; return the ranked list.

    Daemon threads, joined against a shared deadline rather than shut down: a
    source stuck on a cold network keeps running to completion in the background
    (its result is simply ignored) while the caller gets an answer on time, and
    daemon=True means a wedged source can never hold up interpreter exit either.
    """
    budget = timeout_s if timeout_s is not None else _env_float(ENV_TIMEOUT_S, DEFAULT_TIMEOUT_S)
    gathered: dict[str, tuple[Nudge, ...]] = {}
    lock = threading.Lock()

    workers = [
        threading.Thread(
            target=_run_source,
            args=(name, fn, now, gathered, lock),
            name=f"nudge-{name}",
            daemon=True,
        )
        for name, fn in sources
    ]
    for worker in workers:
        worker.start()

    deadline = time.monotonic() + max(0.0, budget)
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))

    with lock:  # a late source may still be writing; take a consistent view
        produced = tuple(n for name, _fn in sources for n in gathered.get(name, ()))
    cap = max(0, _env_int(ENV_MAX, DEFAULT_MAX))
    return sort_nudges(produced)[:cap]


# ---- the default surface -----------------------------------------------------
def _silent_ctx() -> ToolContext:
    """A ToolContext whose ``emit`` goes nowhere.

    A background poll must not push step-events: the overlay renders those as
    "steps taken" for the CURRENT turn, so a nudge refresh would paint phantom
    tool activity mid-conversation and fill the event store with poll noise.
    ``state`` is unused by every read tool composed here.
    """
    return ToolContext(turn_id="nudges", state=None, emit=lambda _event: None)  # type: ignore[arg-type]


def default_sources(*, vault_path: Path | str) -> tuple[NudgeSource, ...]:
    """The four PULL-only reads, built once and reused across polls.

    This list is the ENTIRE surface area of the nudge engine. Nothing here may
    speak, listen, schedule, or connect to the voice socket — every entry is a
    read the user could have made themselves.
    """
    agenda = CalendarAgendaTool()
    gmail = GmailSearchTool()
    todos = TodoListTool(vault_path=vault_path)
    vault = Path(vault_path)

    def calendar_source(now: datetime) -> tuple[Nudge, ...]:
        return meeting_nudges(agenda.run({"days": 1}, _silent_ctx()), now)

    def email_source(now: datetime) -> tuple[Nudge, ...]:
        args = {"query": EMAIL_QUERY, "max_results": _env_int(ENV_EMAIL_MAX, DEFAULT_EMAIL_MAX)}
        return email_nudges(gmail.run(args, _silent_ctx()), now)

    def todo_source(now: datetime) -> tuple[Nudge, ...]:
        args = {"due": "overdue", "source": "user"}
        return todo_nudges(todos.run(args, _silent_ctx()), now)

    def notes_source(now: datetime) -> tuple[Nudge, ...]:
        started, last_saved = read_notes_marks(vault)
        return notes_nudges(started, last_saved, now)

    return (
        ("calendar", calendar_source),
        ("email", email_source),
        ("todo", todo_source),
        ("notes", notes_source),
    )


def empty_snapshot(now: datetime | None = None) -> dict[str, Any]:
    """The "nothing to show" payload — the shape a caller ALWAYS gets, including
    every failure path, so a surface never has to branch on an error."""
    moment = now or datetime.now().astimezone()
    return {
        "generated_at": moment.isoformat(),
        "age_s": 0.0,
        "ttl_s": _env_float(ENV_TTL_S, DEFAULT_TTL_S),
        "count": 0,
        "nudges": [],
    }


class NudgeEngine:
    """Cheap, poll-friendly front door: a TTL cache in front of ``collect``.

    The caller polls (a menu bar ticks every few seconds); the sources must not.
    Between refreshes ``snapshot`` is pure dictionary work, so polling costs
    nothing and can never stall the turn loop. The refresh itself holds the lock
    on purpose: concurrent polls collapse onto ONE round of source calls instead
    of stampeding Google.
    """

    def __init__(
        self,
        *,
        vault_path: Path | str,
        sources: Sequence[NudgeSource] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._sources = (
            tuple(sources) if sources is not None else default_sources(vault_path=vault_path)
        )
        self._lock = threading.Lock()
        self._cached: tuple[Nudge, ...] = ()
        self._generated_at: datetime | None = None

    def snapshot(self) -> dict[str, Any]:
        """The current surface. NEVER raises — an empty list is the failure mode."""
        try:
            return self._refresh_if_stale()
        except Exception as exc:  # noqa: BLE001 — the user's menu bar is not a stack trace
            log.warning("nudge snapshot failed (%s: %s)", type(exc).__name__, exc)
            return empty_snapshot()

    def _refresh_if_stale(self) -> dict[str, Any]:
        ttl = max(0.0, _env_float(ENV_TTL_S, DEFAULT_TTL_S))
        now = self._clock()
        with self._lock:
            age = self._age(now)
            if age is None or age >= ttl:
                self._cached = collect(self._sources, now)
                self._generated_at = now
                age = 0.0
            generated_at, nudges = self._generated_at, self._cached
        return {
            "generated_at": generated_at.isoformat() if generated_at else now.isoformat(),
            "age_s": round(age, 3),
            "ttl_s": ttl,
            "count": len(nudges),
            "nudges": [n.to_dict() for n in nudges],
        }

    def _age(self, now: datetime) -> float | None:
        """Seconds since the cached list was computed, or None if there isn't one."""
        if self._generated_at is None:
            return None
        return max(0.0, (now - self._generated_at).total_seconds())
