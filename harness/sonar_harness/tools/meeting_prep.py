"""meeting.prep — everything you need before ONE specific meeting, in one call.

"Prep me for my 6pm" is four lookups wearing a trench coat: which meeting is it,
who's in it, what have we mailed about lately, and what do my own notes already
say about them. A small model asked to chain calendar.agenda + gmail.search +
rag.search + a vault scan will drop half of them, so — exactly like
``daily_brief`` — this composes the existing read tools deterministically and
hands back one grouped, labelled bundle for the model to narrate.

Composition is pure: it instantiates the same tools the registry uses and calls
their ``run`` with the shared ctx, so each still emits its own step-event and
each still degrades to its own "not connected" hint. A dead source costs its
section, never the turn.

**Deliberately NOT cached.** ``daily.brief`` saves a dated artifact because a
day's shape barely moves; a meeting's context moves right up to the minute it
starts (a reply lands, a guest is added, a note is written). So every ask
re-reads every source, and this tool writes nothing into the vault.

Which meeting the user meant lives in ``meeting_resolve``; the "last time you
met them" scan lives in ``meeting_notes_scan``. What's left here is the tool
itself: the guest list, the mail query, and stitching the sections together.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from sonar_harness.google_auth import GoogleAuthError, build_service
from sonar_harness.tools.base import ToolBase, ToolContext
from sonar_harness.tools.calendar_read import CalendarAgendaTool
from sonar_harness.tools.gmail_read import GmailSearchTool
from sonar_harness.tools.meeting_notes_scan import render_note_hits, scan_meeting_notes
from sonar_harness.tools.meeting_resolve import (
    STOPWORDS,
    Event,
    Hint,
    parse_agenda,
    parse_hint,
    pick_event,
)
from sonar_harness.tools.rag_backend import RagBackend
from sonar_harness.tools.rag_tools import RagSearchTool

log = logging.getLogger("sonar.tools.meeting_prep")

# A week of look-ahead: "prep me for Thursday's standup" is only resolvable if we
# actually looked past today, and a week is the horizon people talk about.
_LOOKAHEAD_DAYS = 7
_AGENDA_MAX = 30

# Mail: loose by design (see the charter's "start broad" rule) and recent, so a
# prep surfaces the live thread rather than the whole relationship.
_MAIL_WINDOW = "newer_than:30d"
_MAIL_MAX = 5
_MAX_PEOPLE = 4        # more OR-ed senders than this stops being a search
_MAX_SUBJECT_WORDS = 3  # keywords are AND-ed; every extra word can only shrink it

_WORD = re.compile(r"[A-Za-z0-9]+")  # case-preserving, for Gmail keywords
_ADDRESS = re.compile(r"[^\s@]+@[^\s@]+")


def _now() -> datetime:
    """Local now. Indirected so tests can pin the day without freezing the clock."""
    return datetime.now().astimezone()


@dataclass(frozen=True)
class Attendee:
    """One guest on the event. Either field may be blank (Google gives both)."""

    name: str = ""
    email: str = ""


# ---- who's in it -------------------------------------------------------------
def _attendees_from(event: dict[str, Any]) -> tuple[Attendee, ...]:
    """Guests worth prepping for: not the user themself, not a room resource."""
    people: list[Attendee] = []
    for raw in event.get("attendees") or []:
        if not isinstance(raw, dict) or raw.get("self") or raw.get("resource"):
            continue
        email = str(raw.get("email", "")).strip()
        name = str(raw.get("displayName", "")).strip()
        if not (email or name):
            continue
        people.append(Attendee(name=name or email.split("@", 1)[0], email=email))
    return tuple(people)


def _fetch_attendees(event_id: str) -> tuple[Attendee, ...]:
    """The event's guest list — one targeted single-event read.

    calendar.agenda's rendered lines carry the time, title and id but not the
    guests, and the guests ARE most of the prep (they drive the mail search and
    the note scan). Never raises: no token, a deleted event, or an API hiccup all
    mean "prep without a guest list", not a failed turn.
    """
    if not event_id:
        return ()
    try:
        service = build_service("calendar", "v3")
        event = service.events().get(calendarId="primary", eventId=event_id).execute()
    except GoogleAuthError:
        return ()  # already surfaced by calendar.agenda's own section
    except Exception as exc:  # noqa: BLE001 — a guest list is never worth a crash
        log.warning("could not read attendees for event %s (%s)", event_id, exc)
        return ()
    return _attendees_from(event if isinstance(event, dict) else {})


# ---- recent mail with them ---------------------------------------------------
def _first_token(name: str) -> str:
    """The given name. ``from:`` can't take a bare two-word name, and quoting it
    would force an exact-phrase match — the one thing the charter forbids."""
    tokens = [t for t in _WORD.findall(name) if len(t) > 1]
    return tokens[0] if tokens else ""


def _plain_addresses(raw: Sequence[str]) -> list[str]:
    """Guest emails that are safe to drop into a query unquoted.

    The guest list is text off an invite, and the charter forbids quoting search
    terms — so anything that isn't shaped like a bare address (a display name
    that landed in the email field, something carrying spaces and therefore Gmail
    operators) is dropped rather than escaped. Losing one weak search term costs
    less than letting invite text rewrite the query.
    """
    return [
        candidate
        for candidate in (item.strip() for item in raw if isinstance(item, str))
        if _ADDRESS.fullmatch(candidate)
    ]


def _subject_keywords(subject: str) -> str:
    """A couple of bare keywords from the meeting title, operators stripped."""
    words = [w for w in _WORD.findall(subject) if len(w) > 2 and w.lower() not in STOPWORDS]
    return " ".join(words[:_MAX_SUBJECT_WORDS])


def mail_query(*, addresses: Sequence[str], names: Sequence[str], subject: str) -> str:
    """A deliberately LOOSE Gmail query for recent threads with these people.

    Addresses beat names beat the meeting's own title, because each is a weaker
    signal than the last. Terms are OR-ed and never quoted: per the charter, a
    quoted paraphrase forces an exact-phrase match and usually finds nothing, and
    every AND-ed keyword can only shrink the result. Each person is matched in
    BOTH directions — what the user sent them is as much context as what they
    sent back.
    """
    people = _plain_addresses(addresses)
    if not people:
        people = [t for t in (_first_token(n) for n in names if isinstance(n, str)) if t]
    if people:
        terms = " OR ".join(f"from:{p} OR to:{p}" for p in people[:_MAX_PEOPLE])
        return f"({terms}) {_MAIL_WINDOW}"
    keywords = _subject_keywords(subject)
    return f"{keywords} {_MAIL_WINDOW}".strip() if keywords else _MAIL_WINDOW


# ---- rendering ---------------------------------------------------------------
def _when_label(event: Event) -> str:
    if event.clock is None:
        return f"{event.day.strftime('%A %d %b')} (all day)"
    return f"{event.day.strftime('%A %d %b')} at {event.clock[0]:02d}:{event.clock[1]:02d}"


def render_meeting(event: Event | None, hint: Hint, agenda: str) -> str:
    """The resolved meeting — or a plain admission plus the real agenda."""
    if event is not None:
        where = f" @ {event.location}" if event.location else ""
        return f"{_when_label(event)} — {event.title}{where}"
    asked = f' matching "{hint.raw}"' if hint.raw else ""
    return (
        f"I couldn't work out which meeting{asked}. Here is what the calendar "
        f"returned for the next {_LOOKAHEAD_DAYS} days — ask the user which one "
        f"they meant rather than guessing:\n{agenda.strip()}"
    )


def render_attendees(attendees: Sequence[Attendee]) -> str:
    if not attendees:
        return (
            "The calendar listed no guests for this one, so the rest of this prep "
            "comes from the meeting's title and the user's own notes."
        )
    return "\n".join(
        f"- {a.name}" + (f" <{a.email}>" if a.email else "") for a in attendees
    )


def render_prep(sections: Sequence[tuple[str, str]]) -> str:
    """Join labelled sections into one model-readable bundle (pure)."""
    return "\n\n".join(
        f"### {label}\n{content.strip()}" for label, content in sections if content.strip()
    )


def _guarded(label: str, produce: Callable[[], str]) -> tuple[str, str]:
    """Run one section's source. A source that breaks its never-raise contract
    costs its own section and nothing else — the prep still ships."""
    try:
        return label, produce()
    except Exception as exc:  # noqa: BLE001 — one dead source must not kill the prep
        log.warning("meeting.prep: %r section failed (%s)", label, exc)
        return label, f"(unavailable — {type(exc).__name__})"


class MeetingPrepTool(ToolBase):
    name = "meeting.prep"
    description = (
        "EVERYTHING the user needs before a specific meeting, in ONE call: which "
        "meeting it is, who's in it, recent email with those people, what their "
        "own notes say about the topic and the people, and what was decided the "
        "last time they met. Use this for ANY 'get me ready' question about a "
        "meeting — 'prep me for my 6pm', 'what do I need for my next meeting', "
        "'who am I meeting with and what's the background', 'brief me before the "
        "standup', 'what did we decide with them last time'. Prefer it over "
        "calling calendar, email or notes tools separately — it already includes "
        "all of them. Pass the user's own words for 'meeting' ('6pm', 'thursday "
        "standup', 'the design review'); leave it out for the next upcoming "
        "event. If the result says the meeting couldn't be resolved, ask the user "
        "which one they meant instead of prepping a different one. Narrate it "
        "warmly and briefly — who and when first, then the background — and do "
        "NOT read ids, email addresses, file paths or raw JSON aloud."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "meeting": {
                "type": "string",
                "description": (
                    "The user's own words for which meeting: a time ('6pm', "
                    "'18:00'), a day ('thursday', 'tomorrow'), words from the "
                    "title ('design review'), or any mix. Omit for the next "
                    "upcoming event."
                ),
            },
        },
        "required": [],
    }
    permission = "local"

    def __init__(self, *, vault_path: str, rag_backend: RagBackend | None = None) -> None:
        self._vault = vault_path
        self._agenda = CalendarAgendaTool()
        self._gmail = GmailSearchTool()
        # None only in a degraded session (no index built); the RAG sections then
        # say so instead of the whole prep failing at construction time.
        self._rag = RagSearchTool(backend=rag_backend) if rag_backend is not None else None

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        hint = parse_hint(args.get("meeting"), today=_now().date())
        agenda = self._agenda_text(ctx)
        event = pick_event(parse_agenda(agenda), hint)
        attendees = _fetch_attendees(event.event_id) if event is not None else ()

        sections = self._compose(
            event=event, attendees=attendees, hint=hint, agenda=agenda, ctx=ctx
        )
        body = render_prep(sections)
        # Count what actually shipped, not what was attempted: render_prep drops
        # a section whose source came back empty, and the overlay should show the
        # user what they got.
        filled = sum(1 for _, content in sections if content.strip())
        subject = f"prepped {event.title!r}" if event is not None else "no meeting resolved"
        ctx.emit(
            {
                "step": "tool_result_summary",
                "tool": self.name,
                "detail": f"{subject} ({filled} sections)",
                "status": "ok",
            }
        )
        return body

    # ---- sources -------------------------------------------------------------
    def _compose(
        self,
        *,
        event: Event | None,
        attendees: Sequence[Attendee],
        hint: Hint,
        agenda: str,
        ctx: ToolContext,
    ) -> list[tuple[str, str]]:
        """Every section we can honestly fill, in the order the model should say
        them. When no event resolved, the user's own phrasing stands in as the
        subject — an unresolvable calendar shouldn't also cost them their notes."""
        subject = event.title if event is not None else hint.raw
        names = tuple(a.name for a in attendees if a.name)
        addresses = tuple(a.email for a in attendees if a.email)
        needles = list(names) or ([subject] if subject else [])

        # Labels say "the user's" rather than "their": read out of context, a
        # small model can hear "their notes" as the ATTENDEES' notes and narrate
        # a source Sonar has no access to.
        sections = [
            ("The meeting", render_meeting(event, hint, agenda)),
            ("Who's in it", render_attendees(attendees)),
            _guarded(
                "Recent email with these people",
                lambda: self._gmail.run(
                    {
                        "query": mail_query(addresses=addresses, names=names, subject=subject),
                        "max_results": _MAIL_MAX,
                    },
                    ctx,
                ),
            ),
        ]
        if subject:
            sections.append(
                _guarded("The user's notes on this topic", lambda: self._rag_search(subject, ctx))
            )
        if names:
            sections.append(
                _guarded(
                    "The user's notes on these people",
                    lambda: self._rag_search(" ".join(names), ctx),
                )
            )
        sections.append(
            _guarded(
                "Last time the user met them",
                lambda: render_note_hits(scan_meeting_notes(self._vault, needles)),
            )
        )
        return sections

    def _agenda_text(self, ctx: ToolContext) -> str:
        """calendar.agenda over a week. Its own failures already come back as
        text; this only catches the unexpected so resolution can still say why."""
        try:
            return self._agenda.run({"days": _LOOKAHEAD_DAYS, "max_results": _AGENDA_MAX}, ctx)
        except Exception as exc:  # noqa: BLE001 — the prep degrades, it doesn't die
            log.warning("meeting.prep: calendar unavailable (%s)", exc)
            return f"The calendar could not be read ({type(exc).__name__})."

    def _rag_search(self, query: str, ctx: ToolContext) -> str:
        if self._rag is None:
            return "(unavailable — the notes index isn't loaded in this session)"
        return self._rag.run({"query": query}, ctx)
