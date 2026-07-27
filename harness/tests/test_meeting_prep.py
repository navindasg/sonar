"""meeting.prep — resolving WHICH meeting, composing the prep, degrading per source.

The tool is PULL-only and deliberately uncached (a meeting's context changes
right up to the minute it starts), so these tests pin three things: the
resolution rules (the next event, or the natural hint the model passes
through), that every section still comes back when Google isn't connected, and
that one broken source only costs its own section.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from sonar_harness.tools import meeting_prep as mp
from sonar_harness.tools.base import ToolContext
from sonar_harness.tools.calendar_read import render_events
from sonar_harness.tools.meeting_notes_scan import (
    NoteHit,
    _section_body,
    render_note_hits,
    scan_meeting_notes,
)
from sonar_harness.tools.meeting_prep import (
    Attendee,
    MeetingPrepTool,
    _attendees_from,
    mail_query,
    render_attendees,
    render_prep,
)
from sonar_harness.tools.meeting_resolve import parse_agenda, parse_hint, pick_event

NOW = datetime(2026, 7, 26, 9, 12)  # a Sunday; Mon = 07-27, Thu = 07-30

# Exactly what calendar.agenda's render_events emits (see calendar_read.py).
AGENDA = "\n".join(
    [
        "[1] 2026-07-27T09:30:00-07:00 — Standup @ Zoom  [id: ev-standup-mon]",
        "[2] 2026-07-27T18:00:00-07:00 — Design review with Acme  [id: ev-design]",
        "[3] 2026-07-30T09:30:00-07:00 — Standup @ Zoom  [id: ev-standup-thu]",
    ]
)


def _ctx(events: list[dict] | None = None) -> ToolContext:
    sink = events if events is not None else []
    return ToolContext(turn_id="t", state=None, emit=sink.append)


def _hint(text: str | None):
    return parse_hint(text, today=NOW.date())


def _pick(text: str | None):
    return pick_event(parse_agenda(AGENDA), _hint(text))


# ---- fakes -------------------------------------------------------------------
class _FakeRag:
    """A RagBackend that answers every search with one canned passage."""

    def __init__(self, snippet: str = "acme wants the new pricing tier") -> None:
        self._snippet = snippet
        self.queries: list[str] = []

    def search(self, query: str, **_kw: Any) -> dict[str, Any]:
        self.queries.append(query)
        return {
            "results": [
                {
                    "source_path": "projects/acme.md",
                    "heading_path": "Acme",
                    "relevance_score": 0.9,
                    "snippet": self._snippet,
                }
            ]
        }

    def note_context(self, path: str, **_kw: Any) -> dict[str, Any]:  # pragma: no cover
        return {"note": {"path": path, "content": ""}}


class _BrokenRag(_FakeRag):
    """A backend that violates the never-raise contract, to prove containment."""

    def search(self, query: str, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("faiss index is gone")


class _FakeAgenda:
    """Stands in for CalendarAgendaTool with a fixed rendered agenda."""

    name = "calendar.agenda"

    def __init__(self, text: str = AGENDA) -> None:
        self._text = text
        self.calls: list[dict] = []

    def run(self, args: dict, ctx: ToolContext) -> str:
        self.calls.append(dict(args))
        ctx.emit({"step": "tool_result_summary", "tool": self.name, "detail": "ok"})
        return self._text


class _FakeGmail:
    """Stands in for GmailSearchTool; records the query it was handed."""

    name = "gmail.search"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, args: dict, ctx: ToolContext) -> str:
        self.queries.append(str(args.get("query", "")))
        ctx.emit({"step": "tool_result_summary", "tool": self.name, "detail": "1 message"})
        return "[1] Re: pricing — alice@acme.com (Fri)\n    numbers attached"


def _notes_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agenda: _FakeAgenda | None = None,
    gmail: _FakeGmail | None = None,
    rag: _FakeRag | None = None,
    attendees: tuple[Attendee, ...] = (),
) -> MeetingPrepTool:
    """A tool with the Google-facing sources faked out and 'now' pinned."""
    monkeypatch.setattr(mp, "_now", lambda: NOW)
    monkeypatch.setattr(mp, "_fetch_attendees", lambda _event_id: attendees)
    if agenda is not None:
        monkeypatch.setattr(mp, "CalendarAgendaTool", lambda: agenda)
    if gmail is not None:
        monkeypatch.setattr(mp, "GmailSearchTool", lambda: gmail)
    return MeetingPrepTool(vault_path=str(tmp_path), rag_backend=rag or _FakeRag())


def _offline_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> MeetingPrepTool:
    """A tool with NO Google token — every Google-backed source degrades."""
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/google_token.json")
    monkeypatch.setattr(mp, "_now", lambda: NOW)
    return MeetingPrepTool(vault_path=str(tmp_path), rag_backend=kw.pop("rag", None) or _FakeRag())


def _write_meeting_note(vault: Path, *, name: str, created: str, speakers: str, body: str) -> Path:
    """A note in the exact shape voice/notes/store.py writes."""
    path = vault / "Sonar" / "Notes" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"created: {created}\n"
        "type: meeting-notes\n"
        f"speakers: {speakers}\n"
        "source: sonar-notes\n"
        "---\n\n"
        f"# {name}\n\n"
        "## AI Overview\n\n"
        f"{body}\n\n"
        "## Transcript\n\n"
        "**Navin** (00:01): hello\n",
        encoding="utf-8",
    )
    return path


# ---- parsing the agenda back -------------------------------------------------
def test_parse_agenda_reads_time_title_location_and_id() -> None:
    events = parse_agenda(AGENDA)
    assert len(events) == 3
    first = events[0]
    assert first.title == "Standup"
    assert first.location == "Zoom"
    assert first.event_id == "ev-standup-mon"
    assert first.day.isoformat() == "2026-07-27"
    assert first.clock == (9, 30)


def test_parse_agenda_ignores_a_degraded_agenda() -> None:
    """No token / no events must yield nothing to resolve, never a fake event."""
    assert parse_agenda("Google is not connected yet. Run `scripts/sonar.sh google-auth`") == ()
    assert parse_agenda("No events in that window.") == ()
    assert parse_agenda("") == ()


def test_parse_agenda_round_trips_calendar_agendas_real_renderer() -> None:
    """The one test that catches a `render_events` change.

    meeting.prep resolves meetings by parsing calendar.agenda's RENDERED text
    (rather than opening a second, drifting calendar path), so it silently stops
    resolving anything if that renderer's line format moves. calendar_read has no
    test of its own that would notice, so drive the real renderer here.
    """
    rendered = render_events(
        [
            {
                "id": "ev-design",
                "summary": "Design review with Acme",
                "location": "Zoom",
                "start": {"dateTime": "2026-07-27T18:00:00-07:00"},
            },
            {"id": "ev-offsite", "summary": "Offsite", "start": {"date": "2026-07-29"}},
        ]
    )
    timed, all_day = parse_agenda(rendered)
    assert timed.title == "Design review with Acme"
    assert timed.location == "Zoom" and timed.event_id == "ev-design"
    assert timed.day.isoformat() == "2026-07-27" and timed.clock == (18, 0)
    assert all_day.title == "Offsite" and all_day.clock is None


def test_parse_agenda_handles_an_all_day_event_without_an_id() -> None:
    (event,) = parse_agenda("[1] 2026-07-28 — Offsite")
    assert event.title == "Offsite" and event.event_id == ""
    assert event.clock is None and event.day.isoformat() == "2026-07-28"


def test_parse_agenda_drops_an_event_render_events_could_not_time() -> None:
    """render_events writes "(no time)" for an event with no start. Inventing a
    date for it would put a phantom at the top of the sort — and "what's my next
    meeting" would answer with it."""
    assert parse_agenda("[1] (no time) — Mystery meeting  [id: ev-x]") == ()
    assert parse_agenda("[1] not-a-date — Mystery  [id: ev-x]") == ()


def test_parse_agenda_keeps_the_minute_of_the_start_time() -> None:
    (event,) = parse_agenda("[1] 2026-07-27T09:45:00-07:00 — Standup")
    assert event.clock == (9, 45)


# ---- resolving WHICH meeting -------------------------------------------------
def test_no_hint_picks_the_next_upcoming_event() -> None:
    assert _pick(None).event_id == "ev-standup-mon"
    assert _pick("  ").event_id == "ev-standup-mon"


def test_clock_hint_matches_the_event_at_that_time() -> None:
    assert _pick("6pm").event_id == "ev-design"
    assert _pick("my 18:00").event_id == "ev-design"


def test_word_hint_matches_the_title() -> None:
    assert _pick("design review").event_id == "ev-design"


def test_weekday_hint_disambiguates_two_identical_meetings() -> None:
    assert _pick("thursday standup").event_id == "ev-standup-thu"
    assert _pick("standup").event_id == "ev-standup-mon"  # ties go to the earliest


def test_tomorrow_is_resolved_against_today() -> None:
    assert _pick("tomorrow's design review").event_id == "ev-design"


def test_today_and_tomorrow_are_dates_not_just_filler() -> None:
    """Pinned without help from title words: NOW is a Sunday, so "tomorrow" is
    Monday's standup and "today" is a day with nothing on it at all."""
    assert _pick("tomorrow").event_id == "ev-standup-mon"
    assert _pick("today") is None
    assert _pick("tonight") is None


def test_an_unmatchable_hint_resolves_to_nothing() -> None:
    """Better to say 'I couldn't find that' than to prep the wrong meeting."""
    assert _pick("budget sync with finance") is None


def test_one_incidental_word_is_not_a_match() -> None:
    """'quarterly board review' shares only the generic 'review' with the design
    review — matching on that would confidently prep the wrong meeting."""
    assert _pick("quarterly board review") is None
    assert _pick("design review") is not None  # a real majority still matches


def test_a_named_day_carries_a_weak_word_match() -> None:
    """Once a day has narrowed the field, one shared word is enough to choose."""
    assert _pick("thursday review").event_id == "ev-standup-thu"


BUSY_THURSDAY = "\n".join(
    [
        "[1] 2026-07-30T09:30:00-07:00 — Standup @ Zoom  [id: ev-standup-thu]",
        "[2] 2026-07-30T15:00:00-07:00 — Dentist  [id: ev-dentist]",
        "[3] 2026-07-30T17:00:00-07:00 — 1:1 with Priya  [id: ev-1on1]",
    ]
)


def test_a_named_day_does_not_license_a_guess_between_several_events() -> None:
    """Naming a day narrows; it does not authorise a guess. If the user ALSO
    named words and none of them land on any event that day, the earliest event
    is not "the one they meant" — it is exactly how you confidently prep the
    wrong room."""
    events = parse_agenda(BUSY_THURSDAY)
    assert pick_event(events, _hint("thursday design review")) is None
    assert pick_event(events, _hint("thursday board sync with acme")) is None


def test_a_named_day_with_one_candidate_still_answers() -> None:
    """With a single event left on the named day there is nothing to get wrong,
    so a hazy title ("the thursday thing") must not cost the user their prep."""
    (only_one,) = parse_agenda(BUSY_THURSDAY.splitlines()[1])
    assert pick_event([only_one], _hint("thursday design review")) is only_one


def test_a_clock_that_pins_one_event_beats_a_hazy_title() -> None:
    """A time is a much stronger signal than the user's memory of the title."""
    events = parse_agenda(BUSY_THURSDAY)
    assert pick_event(events, _hint("my 5pm design review")).event_id == "ev-1on1"


TWO_SYNCS = "\n".join(
    [
        "[1] 2026-07-27T10:00:00-07:00 — Marketing sync @ Zoom  [id: ev-mkt]",
        "[2] 2026-07-27T15:00:00-07:00 — Engineering sync @ Zoom  [id: ev-eng]",
    ]
)


def test_two_different_meetings_matching_equally_well_is_a_refusal() -> None:
    """The failure this tool exists to prevent. With a Marketing sync at 10 and
    an Engineering sync at 3, "the sync" has no right answer — only a 50% one.
    Taking the earlier is a coin flip wearing the costume of an answer."""
    events = parse_agenda(TWO_SYNCS)
    assert pick_event(events, _hint("sync")) is None
    assert pick_event(events, _hint("the sync meeting")) is None
    assert pick_event(events, _hint("zoom")) is None  # the shared location, too


def test_a_tie_between_instances_of_the_SAME_meeting_still_resolves() -> None:
    """The other half of the rule: two events sharing a title are two instances
    of one recurring meeting, and "the standup" means the soonest one."""
    assert _pick("standup").event_id == "ev-standup-mon"


def test_two_events_at_the_same_clock_is_a_refusal() -> None:
    """A clock is an attempt to name ONE meeting; a double-booking at 6pm is
    something only the user can untangle."""
    events = parse_agenda(
        "[1] 2026-07-27T18:00:00-07:00 — Design review  [id: ev-design]\n"
        "[2] 2026-07-27T18:00:00-07:00 — Board call  [id: ev-board]"
    )
    assert pick_event(events, _hint("6pm")) is None
    # …but naming the one they meant resolves it.
    assert pick_event(events, _hint("6pm design review")).event_id == "ev-design"


def test_a_day_is_a_window_not_an_attempt_to_name_one_meeting() -> None:
    """"Prep me for tomorrow" reasonably means the first thing tomorrow, so a day
    on its own is NOT treated as the ambiguity a clock or a title word would be."""
    assert pick_event(parse_agenda(TWO_SYNCS), _hint("monday")).event_id == "ev-mkt"


ONE_ON_ONE = "\n".join(
    [
        "[1] 2026-07-27T09:30:00-07:00 — Standup @ Zoom  [id: ev-standup]",
        "[2] 2026-07-27T17:00:00-07:00 — 1:1 with Priya  [id: ev-1on1]",
    ]
)


def test_a_hint_made_only_of_short_tokens_is_still_a_hint() -> None:
    """"1:1", "Q3" and "P0" are how people actually name meetings. Discarding
    them as too short left the hint looking EMPTY, which silently resolved "prep
    me for my 1:1" to whatever happened to be next."""
    events = parse_agenda(ONE_ON_ONE)
    for phrasing in ("1:1", "my 1:1", "the 1:1"):
        assert pick_event(events, _hint(phrasing)).event_id == "ev-1on1", phrasing


def test_a_short_hint_that_matches_nothing_refuses_rather_than_defaulting() -> None:
    """The same rule in the other direction: an unmatchable short hint must not
    collapse back into "no hint at all" and prep the next thing."""
    assert pick_event(parse_agenda(ONE_ON_ONE), _hint("Q3")) is None


def test_sentence_framing_still_means_the_next_meeting() -> None:
    """The connective rubble a spoken sentence leaves behind ("do", "am", "i")
    must still filter away, or every natural phrasing would refuse."""
    events = parse_agenda(AGENDA)
    for phrasing in ("my next meeting", "what do I need for my next meeting"):
        assert pick_event(events, _hint(phrasing)).event_id == "ev-standup-mon", phrasing


def test_a_named_minute_must_actually_match() -> None:
    """"My 9:45" is not the 9:30 standup — an off-by-fifteen prep is still the
    wrong meeting."""
    assert _pick("9:30am").event_id == "ev-standup-mon"
    assert _pick("9:45am") is None
    assert _pick("6:30pm") is None


def test_an_all_day_block_never_answers_a_named_time() -> None:
    """They said "6pm"; a day-long "Offsite" is not it."""
    events = parse_agenda("[1] 2026-07-27 — Offsite  [id: ev-offsite]")
    assert pick_event(events, _hint("6pm")) is None
    assert pick_event(events, _hint("offsite")).event_id == "ev-offsite"


def test_the_next_meeting_is_the_earliest_by_CLOCK_not_by_listed_order() -> None:
    """calendar.agenda happens to sort by start time; "what's next" must not
    quietly depend on that."""
    events = parse_agenda(
        "[1] 2026-07-27T18:00:00-07:00 — Design review  [id: ev-design]\n"
        "[2] 2026-07-27T09:30:00-07:00 — Standup  [id: ev-standup]"
    )
    assert pick_event(events, _hint(None)).event_id == "ev-standup"


def test_a_hint_word_can_match_the_location() -> None:
    """"The zoom one" is a real way to pick between two meetings."""
    events = parse_agenda(
        "[1] 2026-07-27T09:30:00-07:00 — Standup @ Zoom  [id: ev-zoom]\n"
        "[2] 2026-07-27T15:00:00-07:00 — Retro @ Room 4  [id: ev-room]"
    )
    assert pick_event(events, _hint("zoom")).event_id == "ev-zoom"


# ---- the email query ---------------------------------------------------------
def test_mail_query_prefers_addresses_and_stays_loose() -> None:
    query = mail_query(addresses=["alice@acme.com", "bob@acme.com"], names=[], subject="Design review")
    assert "alice@acme.com" in query and "bob@acme.com" in query
    assert '"' not in query  # quotes force exact-phrase matches (see the charter)
    assert "newer_than" in query


def test_mail_query_searches_both_directions() -> None:
    """What the user SENT them is as much context as what they sent back, so a
    `from:`-only query would silently halve the thread."""
    query = mail_query(addresses=["alice@acme.com"], names=[], subject="")
    assert "from:alice@acme.com" in query and "to:alice@acme.com" in query


def test_mail_query_falls_back_to_names_then_the_subject() -> None:
    query = mail_query(addresses=[], names=["Alice Chen"], subject="Design review")
    # The GIVEN name only: `from:` can't take a bare two-word name, and quoting
    # it to keep the surname would force the exact-phrase match the charter bans.
    assert "from:Alice" in query and "Chen" not in query

    subject_only = mail_query(addresses=[], names=[], subject="Design review")
    assert "design" in subject_only.lower() and "newer_than" in subject_only


def test_mail_query_caps_how_many_words_it_takes_from_a_title() -> None:
    """Subject keywords are AND-ed, so every extra one can only shrink the
    result — a long title must not narrow the search to nothing."""
    query = mail_query(
        addresses=[], names=[], subject="Quarterly planning offsite kickoff review agenda"
    )
    assert len(query.replace(mp._MAIL_WINDOW, "").split()) <= 3


def test_mail_query_drops_an_address_that_could_rewrite_the_query() -> None:
    """The guest list is text off an invite and goes in unquoted, so anything not
    shaped like a bare address is dropped rather than escaped."""
    query = mail_query(
        addresses=["alice@acme.com OR from:ceo@corp.com", "Alice Chen"], names=[], subject=""
    )
    assert "ceo@corp.com" not in query and "OR" not in query
    # …and a real address alongside the junk still gets through.
    mixed = mail_query(
        addresses=["not an address", "bob@acme.com"], names=[], subject=""
    )
    assert "from:bob@acme.com" in mixed and "not an address" not in mixed


# ---- past meeting notes ------------------------------------------------------
def test_scan_meeting_notes_finds_the_last_time_you_met(tmp_path: Path) -> None:
    _write_meeting_note(
        tmp_path,
        name="Design review",
        created="2026-07-20 14:30",
        speakers='["Navin", "Alice Chen"]',
        body="Decided to ship the pricing tier in August.",
    )
    _write_meeting_note(
        tmp_path,
        name="Gardening club",
        created="2026-07-21 10:00",
        speakers='["Navin", "Someone Else"]',
        body="Tomatoes.",
    )

    hits = scan_meeting_notes(tmp_path, ["Alice Chen"])
    assert [h.title for h in hits] == ["Design review"]
    assert "pricing tier" in hits[0].overview
    assert hits[0].path == "Sonar/Notes/Design review.md"
    assert "Alice Chen" in hits[0].speakers


def test_scan_meeting_notes_returns_the_most_recent_first(tmp_path: Path) -> None:
    _write_meeting_note(
        tmp_path, name="Older", created="2026-06-01 09:00",
        speakers='["Alice Chen"]', body="old news",
    )
    _write_meeting_note(
        tmp_path, name="Newer", created="2026-07-22 09:00",
        speakers='["Alice Chen"]', body="fresh news",
    )

    hits = scan_meeting_notes(tmp_path, ["alice"], limit=1)
    assert [h.title for h in hits] == ["Newer"]


def test_scan_meeting_notes_survives_a_vault_without_notes(tmp_path: Path) -> None:
    assert scan_meeting_notes(tmp_path, ["Alice"]) == ()
    assert scan_meeting_notes(tmp_path / "nope", ["Alice"]) == ()


def test_scan_meeting_notes_needs_a_needle(tmp_path: Path) -> None:
    _write_meeting_note(
        tmp_path, name="Design review", created="2026-07-20 14:30",
        speakers='["Alice Chen"]', body="x",
    )
    assert scan_meeting_notes(tmp_path, []) == ()


def test_the_overview_stops_before_the_transcript(tmp_path: Path) -> None:
    """The section after "## AI Overview" is "## Transcript". Running past the
    next heading would paste a whole meeting's raw speech into the prep — which
    the model would then dutifully read aloud."""
    _write_meeting_note(
        tmp_path, name="Design review", created="2026-07-20 14:30",
        speakers='["Alice Chen"]', body="Decided to ship the pricing tier.",
    )
    (hit,) = scan_meeting_notes(tmp_path, ["Alice Chen"])
    assert hit.overview == "Decided to ship the pricing tier."
    assert "hello" not in hit.overview  # the transcript line _write_meeting_note adds


def test_section_body_stops_at_the_next_heading() -> None:
    text = "## AI Overview\n\nwhat we decided\n\n## Transcript\n\nSECRET\n"
    assert _section_body(text, "AI Overview") == "what we decided"
    assert _section_body(text, "Nothing Like This") == ""


def test_scan_meeting_notes_prefers_the_notes_own_title(tmp_path: Path) -> None:
    """store.py names the file from the title, but a renamed file must not lose
    the title the session was actually saved under."""
    path = _write_meeting_note(
        tmp_path, name="Design review", created="2026-07-20 14:30",
        speakers='["Alice Chen"]', body="x",
    )
    path.rename(path.with_name("2026-07-20-untitled.md"))
    (hit,) = scan_meeting_notes(tmp_path, ["Alice Chen"])
    assert hit.title == "Design review"


# ---- composition -------------------------------------------------------------
def test_prep_composes_every_section(tmp_path: Path, monkeypatch) -> None:
    _write_meeting_note(
        tmp_path,
        name="Design review",
        created="2026-07-20 14:30",
        speakers='["Navin", "Alice Chen"]',
        body="Decided to ship the pricing tier in August.",
    )
    gmail = _FakeGmail()
    tool = _notes_tool(
        tmp_path,
        monkeypatch,
        agenda=_FakeAgenda(),
        gmail=gmail,
        attendees=(Attendee(name="Alice Chen", email="alice@acme.com"),),
    )

    out = tool.run({"meeting": "6pm"}, _ctx())
    assert "Design review" in out           # resolved the right meeting
    assert "Alice Chen" in out              # who's in it
    assert "pricing" in out                 # gmail + vault context landed
    assert "2026-07-20" in out              # the last time you met them
    assert "alice@acme.com" in gmail.queries[0]


def test_prep_asks_the_calendar_for_a_window_not_just_today(tmp_path: Path, monkeypatch) -> None:
    """'Thursday standup' is only resolvable if we looked past today."""
    agenda = _FakeAgenda()
    _notes_tool(tmp_path, monkeypatch, agenda=agenda).run({}, _ctx())
    assert agenda.calls[0]["days"] > 1


def test_prep_emits_a_step_event_per_source(tmp_path: Path, monkeypatch) -> None:
    events: list[dict] = []
    tool = _notes_tool(
        tmp_path, monkeypatch, agenda=_FakeAgenda(), gmail=_FakeGmail(),
        attendees=(Attendee(name="Alice Chen", email="alice@acme.com"),),
    )
    tool.run({}, _ctx(events))

    tools_called = {e.get("tool") for e in events}
    assert {"calendar.agenda", "gmail.search", "rag.search", "meeting.prep"} <= tools_called


def test_prep_is_never_cached(tmp_path: Path, monkeypatch) -> None:
    """A meeting's context changes right up to the meeting — no saved artifact,
    and every ask re-reads every source."""
    agenda = _FakeAgenda()
    tool = _notes_tool(tmp_path, monkeypatch, agenda=agenda, gmail=_FakeGmail())
    tool.run({}, _ctx())
    tool.run({}, _ctx())

    assert len(agenda.calls) == 2
    assert not (tmp_path / "Sonar").exists()  # writes nothing into the vault


def test_prep_uses_the_event_title_and_the_people_for_rag(tmp_path: Path, monkeypatch) -> None:
    rag = _FakeRag()
    tool = _notes_tool(
        tmp_path, monkeypatch, agenda=_FakeAgenda(), gmail=_FakeGmail(), rag=rag,
        attendees=(Attendee(name="Alice Chen", email="alice@acme.com"),),
    )
    tool.run({"meeting": "design review"}, _ctx())

    joined = " | ".join(rag.queries)
    assert "Design review" in joined and "Alice Chen" in joined


# ---- degradation -------------------------------------------------------------
def test_prep_without_google_still_answers_from_the_vault(tmp_path: Path, monkeypatch) -> None:
    _write_meeting_note(
        tmp_path,
        name="Design review",
        created="2026-07-20 14:30",
        speakers='["Navin", "Alice Chen"]',
        body="Decided to ship the pricing tier in August.",
    )
    out = _offline_tool(tmp_path, monkeypatch).run({"meeting": "design review"}, _ctx())

    assert "google" in out.lower()      # the calendar/email sections say why
    assert "2026-07-20" in out          # …and the vault sections still landed
    assert "pricing tier" in out


def test_prep_says_plainly_when_it_cannot_find_the_meeting(tmp_path: Path, monkeypatch) -> None:
    out = _offline_tool(tmp_path, monkeypatch).run({}, _ctx())
    assert "couldn't" in out.lower() or "could not" in out.lower()
    assert "google" in out.lower()  # …and hands the model the reason


def test_prep_reports_a_hint_that_matched_nothing(tmp_path: Path, monkeypatch) -> None:
    tool = _notes_tool(tmp_path, monkeypatch, agenda=_FakeAgenda(), gmail=_FakeGmail())
    out = tool.run({"meeting": "budget sync"}, _ctx())
    assert "budget sync" in out
    assert "Standup" in out  # the real upcoming events, so the model can ask


def test_a_broken_source_only_costs_its_own_section(tmp_path: Path, monkeypatch) -> None:
    tool = _notes_tool(
        tmp_path, monkeypatch, agenda=_FakeAgenda(), gmail=_FakeGmail(), rag=_BrokenRag(),
    )
    out = tool.run({}, _ctx())
    assert "Standup" in out          # the meeting section survived
    assert "unavailable" in out      # the RAG section degraded in place


def test_prep_ignores_a_non_string_hint(tmp_path: Path, monkeypatch) -> None:
    tool = _notes_tool(tmp_path, monkeypatch, agenda=_FakeAgenda(), gmail=_FakeGmail())
    out = tool.run({"meeting": 6}, _ctx())
    assert "Standup" in out  # falls back to the next upcoming event


def test_prep_without_a_notes_index_still_preps(tmp_path: Path, monkeypatch) -> None:
    """A session built with no RAG backend loses two sections, not the turn."""
    monkeypatch.setattr(mp, "_now", lambda: NOW)
    monkeypatch.setattr(mp, "CalendarAgendaTool", lambda: _FakeAgenda())
    monkeypatch.setattr(mp, "GmailSearchTool", lambda: _FakeGmail())
    out = MeetingPrepTool(vault_path=str(tmp_path), rag_backend=None).run({}, _ctx())
    assert "Standup" in out and "unavailable" in out


# ---- the guest list ----------------------------------------------------------
def test_attendees_skip_the_user_and_the_room() -> None:
    """'Who's in it' means other people — prepping to meet yourself, or a
    conference room, is noise the model would dutifully read aloud."""
    people = _attendees_from(
        {
            "attendees": [
                {"email": "navin@example.com", "self": True},
                {"email": "boardroom@example.com", "resource": True},
                {"email": "alice@acme.com", "displayName": "Alice Chen"},
                {"email": "bob@acme.com"},  # no display name -> derive one
                {},                          # nothing usable at all
            ]
        }
    )
    assert people == (
        Attendee(name="Alice Chen", email="alice@acme.com"),
        Attendee(name="bob", email="bob@acme.com"),
    )


def test_attendees_of_an_event_without_a_guest_list() -> None:
    assert _attendees_from({}) == () and _attendees_from({"attendees": None}) == ()


# ---- query + render edges ----------------------------------------------------
def test_mail_query_caps_how_many_people_it_ors_together() -> None:
    """Past a handful of OR-ed senders it stops being a search."""
    query = mail_query(
        addresses=[f"p{i}@acme.com" for i in range(9)], names=[], subject="All hands"
    )
    assert query.count("from:") <= 4


def test_mail_query_strips_operators_out_of_a_meeting_title() -> None:
    """A title is untrusted text; it must not smuggle Gmail syntax into the query."""
    query = mail_query(addresses=[], names=[], subject='1:1 "sync" (from:boss)')
    assert '"' not in query and "(" not in query
    assert "newer_than" in query


def test_render_prep_drops_a_section_with_nothing_in_it() -> None:
    out = render_prep([("The meeting", "Standup"), ("Who's in it", "   ")])
    assert "### The meeting" in out and "Who's in it" not in out


def test_render_attendees_names_every_guest() -> None:
    """Asserted directly rather than through a whole prep: the composed output
    also carries the guests' names in the notes section, so an end-to-end
    assertion passes even when this renderer emits nothing but addresses."""
    out = render_attendees(
        [Attendee(name="Alice Chen", email="alice@acme.com"), Attendee(name="Bob")]
    )
    assert "Alice Chen" in out and "alice@acme.com" in out
    assert "Bob" in out
    assert render_attendees([]).startswith("The calendar listed no guests")


def test_render_note_hits_carries_what_was_decided() -> None:
    """Likewise: "what was decided last time" is the whole point of the section,
    and every other source in a prep can mention the same words."""
    out = render_note_hits(
        [
            NoteHit(
                path="Sonar/Notes/Design review.md",
                title="Design review",
                created="2026-07-20 14:30",
                speakers=("Navin", "Alice Chen"),
                overview="Agreed to ship the pricing tier in August.",
            )
        ]
    )
    assert "Agreed to ship the pricing tier in August." in out
    assert "2026-07-20 14:30" in out and "Design review" in out and "Alice Chen" in out
    assert render_note_hits([]) == "No past Sonar meeting notes match."


def test_render_note_hits_truncates_a_runaway_overview() -> None:
    """An overview is model context, not a transcript dump."""
    out = render_note_hits(
        [NoteHit(path="p", title="t", created="c", speakers=("x",), overview="word " * 400)]
    )
    assert len(out) < 800 and out.endswith("…")


def test_parse_agenda_keeps_an_at_sign_inside_a_title() -> None:
    """The location is the LAST ' @ ' segment, so a title may contain one."""
    (event,) = parse_agenda("[1] 2026-07-27T09:30:00-07:00 — Lunch @ Bar @ Cafe Rio")
    assert event.title == "Lunch @ Bar" and event.location == "Cafe Rio"
