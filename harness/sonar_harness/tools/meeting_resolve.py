"""Which meeting did the user mean? — reading calendar.agenda back, and the hint.

``meeting.prep``'s one irreducible decision, split out because it is the part
with all of the risk and none of the I/O: given ``calendar.agenda``'s rendered
text and the user's own phrasing, work out which event they meant — or work out,
out loud, that it can't be told. Everything here is pure, which is what makes it
cheap to test exhaustively.

Resolution is conservative by construction. A hint that matches nothing, or that
matches two DIFFERENT meetings equally well, resolves to ``None``: prepping the
wrong meeting is worse than admitting the miss, and the caller turns a ``None``
into "I couldn't tell which — here's what's actually on".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

# calendar.agenda's rendered line: "[1] <when> — <title>[ @ <loc>][  [id: <id>]]".
_AGENDA_LINE = re.compile(r"^\[\d+\]\s+(?P<body>.+)$")
_ID_TAIL = re.compile(r"\s*\[id:\s*(?P<id>[^\]]*)\]\s*$")
_WHEN = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})(?:T(?P<hour>\d{2}):(?P<minute>\d{2}))?")
_DASH = " — "
_AT = " @ "

# Hint parsing. Times first (so "6pm" never survives as the word "6"), then days.
_CLOCK_12 = re.compile(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?\b")
_CLOCK_24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_TOKEN = re.compile(r"[a-z0-9]+")  # over already-lowercased text

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_RELATIVE_DAYS = {"today": 0, "tonight": 0, "tomorrow": 1}

# Function words plus the framing people wrap a request in. Dropping them is what
# lets "my next meeting" resolve to "the next one" instead of hunting for a
# meeting literally titled "next" — and keeps "with" from matching every event
# titled "<something> with <someone>". Shared with the mail query, which strips
# the same non-identifying words out of a meeting title.
STOPWORDS = frozenset({
    "the", "and", "for", "with", "about", "from", "that", "this", "these",
    "those", "there", "what", "who", "whom", "whose", "when", "which",
    "my", "our", "your", "its", "his", "her", "their", "mine", "ours",
    "next", "last", "upcoming", "coming", "before", "after",
    "prep", "prepare", "brief", "need", "know", "into", "have", "get",
    "meeting", "meetings",
})

_MIN_WORD = 3  # for ALPHABETIC tokens only — see parse_hint


# ---- value types -------------------------------------------------------------
@dataclass(frozen=True)
class Event:
    """One line of ``calendar.agenda`` output, read back into fields."""

    day: date
    clock: tuple[int, int] | None  # (hour, minute) local; None = all-day
    title: str
    location: str
    event_id: str


@dataclass(frozen=True)
class Hint:
    """What the user's phrasing pins down about WHICH meeting they meant."""

    raw: str
    day: date | None
    hour: int | None
    minute: int | None
    words: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        """True when nothing was pinned — i.e. "just my next meeting"."""
        return self.day is None and self.hour is None and not self.words


# ---- reading the agenda back -------------------------------------------------
def _parse_when(when: str) -> tuple[date, tuple[int, int] | None] | None:
    """Split a rendered start ("2026-07-27T18:00:00-07:00" | "2026-07-28")."""
    match = _WHEN.match(when)
    if not match:
        return None
    try:
        day = date.fromisoformat(match.group("day"))
    except ValueError:  # a shape we don't recognise is not an event we can prep
        return None
    if match.group("hour") is None:
        return day, None
    return day, (int(match.group("hour")), int(match.group("minute")))


def parse_agenda(text: str) -> tuple[Event, ...]:
    """Read ``calendar.agenda``'s rendered lines back into structured events.

    calendar.agenda owns the credentials and the API call, so we let it fetch and
    parse its own output rather than maintaining a second calendar path that can
    drift. Anything that isn't a recognisable event line — "No events in that
    window.", the "run google-auth" hint, an event ``render_events`` had to label
    "(no time)", an empty string — yields nothing, which is what makes an
    unresolvable meeting *stay* unresolved instead of guessed.
    """
    events: list[Event] = []
    for raw in text.splitlines():
        match = _AGENDA_LINE.match(raw.strip())
        if not match:
            continue
        body = match.group("body")
        event_id = ""
        tail = _ID_TAIL.search(body)
        if tail:
            event_id = tail.group("id").strip()
            body = body[: tail.start()]
        when, sep, rest = body.partition(_DASH)
        if not sep:
            continue
        parsed = _parse_when(when.strip())
        if parsed is None:
            continue
        day, clock = parsed
        # rpartition, not partition: a title may itself contain " @ ", and the
        # location is always the LAST segment render_events appended.
        head, at, location = rest.rpartition(_AT)
        title = head if at else rest
        events.append(
            Event(
                day=day,
                clock=clock,
                title=title.strip(),
                location=location.strip() if at else "",
                event_id=event_id,
            )
        )
    return tuple(events)


# ---- reading the user's phrasing ---------------------------------------------
def _take_clock(text: str) -> tuple[int | None, int | None, str]:
    """Pull a time out of the hint, returning (hour, minute, remaining text).

    The time is *removed* so it can't also be scored as a title word. A bare
    number ("my 6") is deliberately NOT read as a time — "1:1" and "Q3" live in
    real meeting titles, and a wrong confident match is worse than no match.
    """
    match = _CLOCK_12.search(text)
    if match:
        hour = int(match.group(1)) % 12 + (12 if match.group(3) == "p" else 0)
        minute = int(match.group(2)) if match.group(2) else None
        return hour, minute, f"{text[: match.start()]} {text[match.end():]}"
    match = _CLOCK_24.search(text)
    if match:
        rest = f"{text[: match.start()]} {text[match.end():]}"
        return int(match.group(1)), int(match.group(2)), rest
    return None, None, text


def _take_day(text: str, today: date) -> tuple[date | None, str]:
    """Pull "today"/"tomorrow"/a weekday out of the hint, resolved against ``today``."""
    for token in _TOKEN.findall(text):
        if token in _RELATIVE_DAYS:
            return today + timedelta(days=_RELATIVE_DAYS[token]), _drop_word(text, token)
        if token in _WEEKDAYS:
            # The soonest such weekday including today: the agenda already starts
            # "now", so "thursday" said on a Thursday means the one still ahead.
            ahead = (_WEEKDAYS[token] - today.weekday()) % 7
            return today + timedelta(days=ahead), _drop_word(text, token)
    return None, text


def _drop_word(text: str, word: str) -> str:
    return re.sub(rf"\b{re.escape(word)}\b", " ", text)


def parse_hint(text: str | None, *, today: date) -> Hint:
    """Turn "tomorrow's 6pm design review" into the constraints we can match on.

    Non-string / blank input is a valid answer, not an error: it means "the next
    upcoming meeting", which is what "what do I need for my next meeting" wants.

    The length floor applies to ALPHABETIC tokens only. It is there to drop the
    connective rubble a spoken sentence leaves behind ("do", "am", "i") — but
    "1:1", "Q3", "P0" and "v2" are how people actually name meetings, and
    discarding those left the hint looking EMPTY, which resolved "prep me for my
    1:1" to whatever happened to be next. A token carrying a digit is signal.
    """
    raw = text.strip() if isinstance(text, str) else ""
    hour, minute, rest = _take_clock(raw.lower())
    day, rest = _take_day(rest, today)
    words = tuple(
        token
        for token in _TOKEN.findall(rest)
        if token not in STOPWORDS and (len(token) >= _MIN_WORD or not token.isalpha())
    )
    return Hint(raw=raw, day=day, hour=hour, minute=minute, words=words)


# ---- choosing -----------------------------------------------------------------
def _starts_at(event: Event) -> tuple[date, tuple[int, int]]:
    """Sort key. All-day events sort to the top of their day, as they should."""
    return event.day, event.clock or (0, 0)


def _matches_when(event: Event, hint: Hint) -> bool:
    """Hard filter: an explicit day/time the user gave must actually hold."""
    if hint.day is not None and event.day != hint.day:
        return False
    if hint.hour is None:
        return True
    if event.clock is None:  # they named a time; an all-day block isn't it
        return False
    if event.clock[0] != hint.hour:
        return False
    return hint.minute is None or event.clock[1] == hint.minute


def _word_hits(event: Event, words: Sequence[str]) -> int:
    """How many of the hint's words show up in the event's title or location."""
    haystack = f"{event.title} {event.location}".lower()
    tokens = set(_TOKEN.findall(haystack))
    # Whole-token first; a substring fallback only for words long enough that an
    # accidental match is implausible (so "review" still finds "reviews").
    return sum(1 for w in words if w in tokens or (len(w) >= 4 and w in haystack))


def _unambiguous(candidates: Sequence[Event]) -> Event | None:
    """The soonest candidate — but only if they are all the SAME meeting.

    Two survivors sharing a title are two instances of one recurring meeting, and
    the soonest is exactly what "the standup" means. Two survivors with DIFFERENT
    titles fit the hint equally well, and picking the earlier one is a coin flip
    wearing the costume of an answer: "the sync" with a Marketing sync at 10 and
    an Engineering sync at 3 has no right answer, only a 50% one. Refuse, and let
    the caller ask which.
    """
    if not candidates:
        return None
    if len({event.title.strip().lower() for event in candidates}) > 1:
        return None
    return candidates[0]


def pick_event(events: Sequence[Event], hint: Hint) -> Event | None:
    """Choose the meeting the user meant, or ``None`` if we honestly can't tell.

    Order of authority: an explicit day/time is a hard filter, title words break
    the remaining tie, and a tie between two DIFFERENT meetings is refused rather
    than broken. Returning ``None`` is a first-class outcome — the caller reports
    the miss and lists the real agenda rather than prepping the wrong room.
    """
    ordered = sorted(events, key=_starts_at)
    if not ordered:
        return None
    if hint.is_empty:
        return ordered[0]

    pinned = [event for event in ordered if _matches_when(event, hint)]
    if not pinned:
        return None
    if not hint.words:
        # A day is a WINDOW — "prep me for tomorrow" reasonably means the first
        # thing tomorrow. A clock is an attempt to name ONE meeting, so two
        # events sharing it is a double-booking only the user can resolve.
        return pinned[0] if hint.hour is None else _unambiguous(pinned)

    scored = [(event, _word_hits(event, hint.words)) for event in pinned]
    best = max(hits for _, hits in scored)
    narrowed = hint.day is not None or hint.hour is not None
    if best == 0:
        # The user named words and NONE of them landed. A day or a clock has
        # already narrowed the field, so with exactly one candidate left there is
        # nothing to get wrong — "the thursday thing" shouldn't cost them their
        # prep. With several still standing, taking the earliest is a guess
        # dressed up as an answer, and prepping the wrong room is the one failure
        # this tool exists to avoid. Say so instead.
        return pinned[0] if narrowed and len(pinned) == 1 else None
    # Words alone must carry a MAJORITY of what the user said. One incidental
    # word ("quarterly board review" vs "Design review") is how you confidently
    # prep the wrong meeting; once a day or a clock has narrowed the field, a
    # single shared word is enough to break the remaining tie.
    if not narrowed and best * 2 < len(hint.words):
        return None
    return _unambiguous([event for event, hits in scored if hits == best])
