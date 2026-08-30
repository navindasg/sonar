"""weekly.review — week boundaries, the five sections, and the saved artifact.

Two things need pinning hard here. First the WEEK BOUNDARY: "how did my week go"
is only meaningful if "the week" means one specific thing, so every test drives a
frozen, injected clock and asserts Monday-to-Monday (ISO) semantics — including
the nasty ISO year-boundary case where late December belongs to the *previous*
ISO year. Second the read-back-first artifact: the first ask of a week composes
and saves ``Sonar/Review/<iso-week>.md``, every later ask that week reads it back,
and ``refresh`` is the only way to recompute. Same contract as daily.brief, one
resolution up.
"""

from __future__ import annotations

import os
import time as time_module
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sonar_harness.state import State
from sonar_harness.tools import weekly_review as wr
from sonar_harness.tools.base import ToolContext
from sonar_harness.tools.weekly_review import (
    WeeklyReviewTool,
    completion_day,
    drop_declined,
    fetch_meetings,
    parse_review_note,
    render_review_note,
    review_path,
    scan_briefs,
    scan_completed_checkboxes,
    scan_notes,
    week_bounds,
    week_key,
)

# Wednesday of ISO week 2026-W31 (Mon 2026-07-27 .. Sun 2026-08-02).
NOW = datetime(2026, 7, 29, 17, 30).astimezone()
# The Monday AFTER that week — when "how did my week go" means the week just ended.
NEXT_MONDAY_MORNING = datetime(2026, 8, 3, 9, 0).astimezone()
THIS_MONDAY = date(2026, 7, 27)
LAST_MONDAY = date(2026, 7, 20)


@pytest.fixture
def clock_behind_utc():
    """Pin the process clock to a zone genuinely BEHIND UTC.

    Without this the local-vs-UTC assertions below would pass for free on a
    machine already running in UTC (or east of it), which is exactly the kind of
    test that looks green and pins nothing.
    """
    before = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time_module.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time_module.tzset()


def _ctx(events: list[dict] | None = None, state: State | None = None) -> ToolContext:
    sink = events if events is not None else []
    return ToolContext(turn_id="t", state=state, emit=sink.append)


def _tool(vault: Path, *, now: datetime = NOW, monkeypatch) -> WeeklyReviewTool:
    """A tool on a frozen clock with no Google token, so calendar degrades."""
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/google_token.json")
    return WeeklyReviewTool(vault_path=str(vault), now=lambda: now)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---- week boundary (the thing everything else hangs off) ---------------------
def test_week_runs_monday_to_monday_exclusive() -> None:
    start, end = week_bounds(date(2026, 7, 29))
    assert start == THIS_MONDAY
    assert end == date(2026, 8, 3)  # exclusive: the NEXT Monday


def test_monday_belongs_to_its_own_week_and_sunday_to_the_one_before() -> None:
    assert week_bounds(THIS_MONDAY)[0] == THIS_MONDAY          # Monday starts it
    assert week_bounds(date(2026, 8, 2))[0] == THIS_MONDAY     # Sunday still in it
    assert week_bounds(date(2026, 7, 26))[0] == LAST_MONDAY    # the Sunday before


def test_weeks_back_shifts_a_whole_week() -> None:
    assert week_bounds(date(2026, 7, 29), weeks_back=1) == (
        LAST_MONDAY,
        THIS_MONDAY,
    )


def test_week_key_is_the_iso_year_and_week() -> None:
    assert week_key(THIS_MONDAY) == "2026-W31"
    assert week_key(LAST_MONDAY) == "2026-W30"


def test_week_key_uses_the_iso_year_not_the_calendar_year() -> None:
    """2027-01-01 is a Friday, so its week is 2026-W53 — keying it '2027-W01'
    would collide with the real 2027-W01 a week later."""
    assert week_key(week_bounds(date(2027, 1, 1))[0]) == "2026-W53"


def test_review_path_is_the_iso_week_note(tmp_path: Path) -> None:
    assert review_path(tmp_path, THIS_MONDAY) == (
        tmp_path / "Sonar" / "Review" / "2026-W31.md"
    )


# ---- meetings ----------------------------------------------------------------
def test_drop_declined_keeps_only_meetings_that_happened() -> None:
    events = [
        {"summary": "kept — no attendees"},
        {"summary": "kept — I accepted", "attendees": [{"self": True, "responseStatus": "accepted"}]},
        {"summary": "gone — I declined", "attendees": [{"self": True, "responseStatus": "declined"}]},
        {"summary": "kept — someone else declined", "attendees": [{"responseStatus": "declined"}]},
        {"summary": "gone — cancelled", "status": "cancelled"},
    ]
    assert [e["summary"] for e in drop_declined(events)] == [
        "kept — no attendees",
        "kept — I accepted",
        "kept — someone else declined",
    ]


def test_drop_declined_does_not_mutate_its_input() -> None:
    events = [{"summary": "a", "attendees": [{"self": True, "responseStatus": "declined"}]}]
    drop_declined(events)
    assert len(events) == 1  # caller's list untouched


class _FakeCalendar:
    """The two-call slice of the Google client this tool actually uses."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items
        self.query: dict = {}

    def events(self) -> "_FakeCalendar":
        return self

    def list(self, **kwargs) -> "_FakeCalendar":
        self.query = kwargs
        return self

    def execute(self) -> dict:
        return {"items": self._items}


def test_fetch_meetings_queries_the_window_and_renders_what_happened(monkeypatch) -> None:
    """The connected path, not just the 'no Google token' hint: without this the
    Meetings section would only ever be exercised while degraded."""
    fake = _FakeCalendar(
        [
            {"summary": "standup", "start": {"dateTime": "2026-07-28T09:00:00-07:00"}},
            {
                "summary": "the one I ducked",
                "start": {"dateTime": "2026-07-28T15:00:00-07:00"},
                "attendees": [{"self": True, "responseStatus": "declined"}],
            },
        ]
    )
    monkeypatch.setattr(wr, "build_service", lambda api, version: fake)

    start, end = datetime(2026, 7, 27).astimezone(), NOW
    out = fetch_meetings(start, end)

    assert "standup" in out
    assert "ducked" not in out  # declined meetings were not part of the week
    assert fake.query["timeMin"] == start.isoformat()
    assert fake.query["timeMax"] == end.isoformat()
    assert fake.query["singleEvents"] is True  # recurring events expanded per-instance


def test_fetch_meetings_turns_an_api_failure_into_text_the_model_can_read(monkeypatch) -> None:
    class _Exploding(_FakeCalendar):
        def execute(self) -> dict:
            raise RuntimeError("calendar exploded")

    monkeypatch.setattr(wr, "build_service", lambda api, version: _Exploding([]))
    out = fetch_meetings(datetime(2026, 7, 27).astimezone(), NOW)
    assert out.startswith("error:") and "calendar exploded" in out


# ---- completed vault checkboxes ---------------------------------------------
def test_completed_checkboxes_use_the_tasks_plugin_done_marker(tmp_path: Path) -> None:
    _write(tmp_path / "work.md", "- [x] shipped the parser ✅ 2026-07-28\n")
    done = scan_completed_checkboxes(tmp_path, THIS_MONDAY, date(2026, 8, 3))
    assert [d["task"] for d in done] == ["shipped the parser ✅ 2026-07-28"]
    assert done[0]["date"] == "2026-07-28"


def test_completed_checkboxes_fall_back_to_the_daily_note_date(tmp_path: Path) -> None:
    _write(tmp_path / "2026-07-28.md", "- [x] called the bank\n")
    done = scan_completed_checkboxes(tmp_path, THIS_MONDAY, date(2026, 8, 3))
    assert [d["task"] for d in done] == ["called the bank"]


def test_completed_checkboxes_outside_the_week_are_excluded(tmp_path: Path) -> None:
    _write(tmp_path / "work.md", "- [x] last week's win ✅ 2026-07-21\n")
    _write(tmp_path / "undated.md", "- [x] no idea when\n")  # undated -> not this week
    assert scan_completed_checkboxes(tmp_path, THIS_MONDAY, date(2026, 8, 3)) == []


def test_open_checkboxes_are_not_counted_as_completed(tmp_path: Path) -> None:
    _write(tmp_path / "2026-07-28.md", "- [ ] still to do\n")
    assert scan_completed_checkboxes(tmp_path, THIS_MONDAY, date(2026, 8, 3)) == []


def test_task_label_drops_the_plugin_markers_it_already_reports() -> None:
    """The bullet prints the date itself, so leaving '✅ 2026-07-28' on the text
    would say it twice — and the model reads this aloud."""
    assert wr.task_label("ship the parser ✅ 2026-07-28") == "ship the parser"
    assert wr.task_label("write the RFC 📅 2026-07-15 ✅ 2026-07-28") == "write the RFC"
    assert wr.task_label("plain task") == "plain task"


def test_task_label_keeps_something_to_say_for_a_marker_only_task() -> None:
    """'- [x] ✅ 2026-07-28' is degenerate, but stripping it to nothing would
    render a bullet with a blank where the task should be."""
    assert wr.task_label("✅ 2026-07-28") == "✅ 2026-07-28"


def test_a_completed_task_is_not_dated_twice_in_the_review(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / "work.md", "- [x] ship the parser ✅ 2026-07-28\n")
    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    assert "- 2026-07-28 — ship the parser (work.md)" in out


def test_the_scan_still_records_the_task_text_verbatim(tmp_path: Path) -> None:
    """Stripping is a rendering concern — provenance keeps the raw line."""
    _write(tmp_path / "work.md", "- [x] ship the parser ✅ 2026-07-28\n")
    done = scan_completed_checkboxes(tmp_path, THIS_MONDAY, date(2026, 8, 3))
    assert done[0]["task"] == "ship the parser ✅ 2026-07-28"


def test_completed_checkboxes_record_where_they_came_from(tmp_path: Path) -> None:
    _write(tmp_path / "Sonar" / "Inbox.md", "- [x] sonar's own ✅ 2026-07-28\n")
    _write(tmp_path / "mine.md", "- [x] my own ✅ 2026-07-28\n")
    sources = {d["source"] for d in scan_completed_checkboxes(tmp_path, THIS_MONDAY, date(2026, 8, 3))}
    assert sources == {"sonar", "user"}


# ---- notes taken -------------------------------------------------------------
def test_notes_are_dated_by_their_frontmatter(tmp_path: Path) -> None:
    _write(
        tmp_path / "Sonar" / "Notes" / "Standup.md",
        "---\ncreated: 2026-07-28 09:05\ntype: meeting-notes\n---\n\n# Standup\n",
    )
    notes = scan_notes(tmp_path, THIS_MONDAY, date(2026, 8, 3))
    assert [(n["date"], n["title"]) for n in notes] == [("2026-07-28", "Standup")]


def test_notes_outside_the_week_are_excluded(tmp_path: Path) -> None:
    _write(
        tmp_path / "Sonar" / "Notes" / "Old.md",
        "---\ncreated: 2026-07-21 09:05\n---\n\n# Old\n",
    )
    assert scan_notes(tmp_path, THIS_MONDAY, date(2026, 8, 3)) == []


def test_notes_without_frontmatter_fall_back_to_the_file_mtime(tmp_path: Path) -> None:
    path = _write(tmp_path / "Sonar" / "Notes" / "Bare.md", "# Bare\n")
    stamp = datetime(2026, 7, 30, 11, 0).timestamp()
    os.utime(path, (stamp, stamp))
    assert [n["date"] for n in scan_notes(tmp_path, THIS_MONDAY, date(2026, 8, 3))] == [
        "2026-07-30"
    ]


# ---- daily briefs ------------------------------------------------------------
def test_briefs_list_the_days_that_have_one_with_their_stamp(tmp_path: Path) -> None:
    _write(
        tmp_path / "Sonar" / "Brief" / "2026-07-28.md",
        "# Morning Brief — 2026-07-28\n\n_generated 09:12 by Sonar_\n\n### Today's calendar\nx\n",
    )
    _write(tmp_path / "Sonar" / "Brief" / "2026-07-20.md", "# old\n")  # last week
    briefs = scan_briefs(tmp_path, THIS_MONDAY, date(2026, 8, 3))
    assert [(b["date"], b["generated_at"]) for b in briefs] == [("2026-07-28", "09:12")]


# ---- compose + save (first ask of the week) ---------------------------------
def test_first_ask_composes_every_section(tmp_path: Path, monkeypatch) -> None:
    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    for label in ("Meetings", "completed", "still open", "Notes taken", "briefs"):
        assert label.lower() in out.lower(), label
    assert "google" in out.lower()  # calendar degrades to its auth hint


def test_first_ask_states_the_window_it_covers(tmp_path: Path, monkeypatch) -> None:
    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    assert "2026-07-27" in out and "2026-07-29" in out  # Monday through today


def _recorded_window(vault: Path, args: dict, monkeypatch) -> tuple[datetime, datetime]:
    """Run the tool with the calendar stubbed, and hand back the window it asked for."""
    seen: list[tuple[datetime, datetime]] = []

    def fake(start: datetime, end: datetime) -> str:
        seen.append((start, end))
        return "No events in that window."

    monkeypatch.setattr(wr, "fetch_meetings", fake)
    _tool(vault, monkeypatch=monkeypatch).run(args, _ctx())
    return seen[0]


def test_the_calendar_window_starts_at_monday_midnight_and_stops_now(
    tmp_path: Path, monkeypatch
) -> None:
    """The injected clock — not datetime.now() — decides which meetings count."""
    start, end = _recorded_window(tmp_path, {}, monkeypatch)
    assert start == datetime(2026, 7, 27, 0, 0).astimezone()
    assert end == NOW  # never asks the calendar about days that haven't happened


def test_the_calendar_window_for_last_week_is_the_whole_week(
    tmp_path: Path, monkeypatch
) -> None:
    start, end = _recorded_window(tmp_path, {"week": "last"}, monkeypatch)
    assert start == datetime(2026, 7, 20, 0, 0).astimezone()
    assert end == datetime(2026, 7, 27, 0, 0).astimezone()  # exclusive next Monday


def test_first_ask_saves_the_iso_week_note(tmp_path: Path, monkeypatch) -> None:
    _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    note = review_path(tmp_path, THIS_MONDAY)
    assert note.exists()
    body = note.read_text(encoding="utf-8")
    assert "2026-W31" in body and "2026-07-29 17:30" in body
    assert not list(note.parent.glob("*.tmp"))  # atomic write left no scratch file


def test_first_ask_surfaces_real_vault_activity(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / "work.md", "- [x] shipped the parser ✅ 2026-07-28\n")
    _write(tmp_path / "2026-07-01.md", "- [ ] pay rent")  # overdue -> still open
    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    assert "shipped the parser" in out
    assert "pay rent" in out


# ---- the two to-do systems ---------------------------------------------------
def test_assistant_todos_finished_this_week_are_reported(tmp_path: Path, monkeypatch) -> None:
    state = State.open(db_path=tmp_path / "s.sqlite")
    # Stored exactly the way todo_done writes it: ISO-8601 *UTC*.
    done_at = (NOW - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    state.conn.execute(
        "INSERT INTO todos (created_at, text, status, done_at) VALUES (?,?, 'done', ?)",
        (done_at, "email the recruiter", done_at),
    )
    state.conn.commit()

    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx(state=state))
    assert "email the recruiter" in out
    state.close()


def test_assistant_todos_finished_before_the_week_are_not(tmp_path: Path, monkeypatch) -> None:
    state = State.open(db_path=tmp_path / "s.sqlite")
    done_at = (NOW - timedelta(days=9)).astimezone(timezone.utc).isoformat()
    state.conn.execute(
        "INSERT INTO todos (created_at, text, status, done_at) VALUES (?,?, 'done', ?)",
        (done_at, "ancient history", done_at),
    )
    state.conn.commit()

    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx(state=state))
    assert "ancient history" not in out
    state.close()


def test_the_review_never_expires_the_assistants_todos(tmp_path: Path, monkeypatch) -> None:
    """A read-only review must not delete rows — expire_todos() is a write."""
    state = State.open(db_path=tmp_path / "s.sqlite")
    stale = (NOW - timedelta(days=30)).astimezone(timezone.utc).isoformat()
    state.conn.execute(
        "INSERT INTO todos (created_at, text, status) VALUES (?,?, 'open')",
        (stale, "very old open todo"),
    )
    state.conn.commit()

    _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx(state=state))
    assert state.conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 1
    state.close()


def test_a_completion_stamp_is_dated_by_the_users_clock_not_utc(clock_behind_utc) -> None:
    """``done_at`` is stored UTC; the week under review is LOCAL. Slicing the raw
    string would name the wrong day for anyone west of Greenwich."""
    # 2026-07-30 04:30 UTC is 2026-07-29 21:30 in Los Angeles — a Wednesday finish.
    assert completion_day("2026-07-30T04:30:00+00:00") == "2026-07-29"


def test_a_completion_stamp_never_lands_outside_the_week_it_is_filed_under(
    tmp_path: Path, monkeypatch, clock_behind_utc
) -> None:
    """A task finished late on the week's last local evening is inside the
    window — so it must not print a date the review says it does not cover."""
    state = State.open(db_path=tmp_path / "s.sqlite")
    done_at = datetime(2026, 8, 2, 23, 30).astimezone().astimezone(timezone.utc).isoformat()
    state.conn.execute(
        "INSERT INTO todos (created_at, text, status, done_at) VALUES (?,?, 'done', ?)",
        (done_at, "late sunday task", done_at),
    )
    state.conn.commit()

    out = _tool(
        tmp_path, now=datetime(2026, 8, 3, 9, 0).astimezone(), monkeypatch=monkeypatch
    ).run({"week": "last"}, _ctx(state=state))
    state.close()
    assert "2026-08-02 — late sunday task" in out


def test_an_unparseable_completion_stamp_degrades_instead_of_raising() -> None:
    """DB drift must cost a pretty date, not the whole review."""
    assert completion_day("2026-07-30") == "2026-07-30"  # date-only row: taken as-is
    assert completion_day("garbled") == "garbled"  # unreadable: hand back what's there


def test_a_missing_state_handle_degrades_instead_of_crashing(tmp_path: Path, monkeypatch) -> None:
    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx(state=None))
    assert "still open" in out.lower()  # composed anyway


# ---- read back (every later ask that week) ----------------------------------
def test_later_ask_reads_the_saved_note_back(tmp_path: Path, monkeypatch) -> None:
    tool = _tool(tmp_path, monkeypatch=monkeypatch)
    tool.run({}, _ctx())
    _write(tmp_path / "work.md", "- [x] appeared later ✅ 2026-07-29\n")

    out = tool.run({}, _ctx())
    assert "appeared later" not in out
    assert "Meetings" in out  # the saved content still comes back


def test_read_back_says_when_it_was_prepared(tmp_path: Path, monkeypatch) -> None:
    tool = _tool(tmp_path, monkeypatch=monkeypatch)
    tool.run({}, _ctx())
    later = _tool(tmp_path, now=NOW + timedelta(days=2), monkeypatch=monkeypatch)
    out = later.run({}, _ctx())
    assert "2026-07-29 17:30" in out  # a week-old cache must date itself, not just time it
    assert "refresh" in out.lower()


def test_read_back_skips_the_source_tools(tmp_path: Path, monkeypatch) -> None:
    """The point of the cache: no vault scans on a re-ask.

    Asserted on ``todo_list``, which composing genuinely emits — an assertion
    about ``calendar.agenda`` would pass for free, since this tool queries the
    calendar directly and never dispatches that tool at all.
    """
    first: list[dict] = []
    tool = _tool(tmp_path, monkeypatch=monkeypatch)
    tool.run({}, _ctx(first))
    assert "todo_list" in {e.get("tool") for e in first}  # composing does scan

    events: list[dict] = []
    tool.run({}, _ctx(events))
    assert "todo_list" not in {e.get("tool") for e in events}


def test_a_finished_week_is_recomposed_past_a_mid_week_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """The Monday question this tool exists for.

    Ask mid-week and the saved note covers Monday-to-Wednesday. Ask "how did last
    week go" the following Monday and that same ISO-week note is the cache key —
    serving it back would report three days as seven and silently lose Thursday
    and Friday.
    """
    _write(tmp_path / "wed.md", "- [x] wednesday thing ✅ 2026-07-29\n")
    _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())  # caches Mon..Wed

    _write(tmp_path / "fri.md", "- [x] friday thing ✅ 2026-07-31\n")
    out = _tool(tmp_path, now=NEXT_MONDAY_MORNING, monkeypatch=monkeypatch).run(
        {"week": "last"}, _ctx()
    )
    assert "friday thing" in out
    assert "the full week" in out  # and it says so, rather than "through today"


def test_a_finished_weeks_review_is_read_back_not_recomputed_every_time(
    tmp_path: Path, monkeypatch
) -> None:
    """The other half: once a week is over its window stops growing, so a review
    composed after it ended is final and must still be served from cache."""
    _write(tmp_path / "fri.md", "- [x] friday thing ✅ 2026-07-31\n")
    monday = _tool(tmp_path, now=NEXT_MONDAY_MORNING, monkeypatch=monkeypatch)
    monday.run({"week": "last"}, _ctx())

    _write(tmp_path / "sneak.md", "- [x] snuck in after ✅ 2026-07-30\n")
    out = _tool(
        tmp_path, now=NEXT_MONDAY_MORNING + timedelta(days=1), monkeypatch=monkeypatch
    ).run({"week": "last"}, _ctx())
    assert "snuck in after" not in out  # served from the saved note
    assert "prepared on 2026-08-03" in out


def test_a_mid_week_re_ask_of_the_current_week_still_reads_back(
    tmp_path: Path, monkeypatch
) -> None:
    """The week in progress keeps daily.brief's read-back contract outright: the
    recompute rule applies only once the window has stopped growing."""
    tool = _tool(tmp_path, monkeypatch=monkeypatch)
    tool.run({}, _ctx())
    _write(tmp_path / "later.md", "- [x] added later ✅ 2026-07-29\n")

    out = _tool(
        tmp_path, now=NOW + timedelta(days=1), monkeypatch=monkeypatch
    ).run({}, _ctx())
    assert "added later" not in out


def test_last_weeks_note_does_not_count_as_this_weeks(tmp_path: Path, monkeypatch) -> None:
    stale = review_path(tmp_path, LAST_MONDAY)
    _write(stale, "# Weekly Review — 2026-W30\n\nlast week's news\n")

    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    assert "last week's news" not in out
    assert review_path(tmp_path, THIS_MONDAY).exists()


# ---- week selection ----------------------------------------------------------
def test_week_last_targets_the_previous_iso_week(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / "work.md", "- [x] last week's win ✅ 2026-07-21\n")
    out = _tool(tmp_path, monkeypatch=monkeypatch).run({"week": "last"}, _ctx())
    assert "last week's win" in out
    assert review_path(tmp_path, LAST_MONDAY).exists()
    assert not review_path(tmp_path, THIS_MONDAY).exists()


def test_week_last_covers_the_whole_week_not_week_to_date(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / "work.md", "- [x] friday win ✅ 2026-07-24\n")
    out = _tool(tmp_path, monkeypatch=monkeypatch).run({"week": "last"}, _ctx())
    assert "2026-07-20" in out and "2026-07-26" in out  # Monday..Sunday, whole week
    assert "friday win" in out


def test_an_unknown_week_value_is_refused_not_guessed(tmp_path: Path, monkeypatch) -> None:
    out = _tool(tmp_path, monkeypatch=monkeypatch).run({"week": "next"}, _ctx())
    assert out.startswith("error:")


# ---- refresh -----------------------------------------------------------------
def test_refresh_recomposes_and_overwrites(tmp_path: Path, monkeypatch) -> None:
    tool = _tool(tmp_path, monkeypatch=monkeypatch)
    tool.run({}, _ctx())
    _write(tmp_path / "work.md", "- [x] added after ✅ 2026-07-29\n")

    out = tool.run({"refresh": True}, _ctx())
    assert "added after" in out
    saved = review_path(tmp_path, THIS_MONDAY).read_text(encoding="utf-8")
    assert "added after" in saved


def test_unreadable_note_falls_back_to_composing(tmp_path: Path, monkeypatch) -> None:
    """A corrupt artifact must never cost the user their review."""
    note = review_path(tmp_path, THIS_MONDAY)
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes(b"\xff\xfe not valid utf-8")

    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    assert "Meetings" in out


def test_a_read_only_vault_still_returns_the_review(tmp_path: Path, monkeypatch) -> None:
    """Saving is best-effort: losing the cache must not lose the answer."""
    vault = tmp_path / "vault"
    (vault / "Sonar").mkdir(parents=True)
    (vault / "Sonar" / "Review").write_text("not a directory", encoding="utf-8")

    events: list[dict] = []
    out = _tool(vault, monkeypatch=monkeypatch).run({}, _ctx(events))
    assert "Meetings" in out
    assert "not saved" in events[-1]["detail"]  # the failure is reported, not hidden


# ---- artifact round-trip -----------------------------------------------------
def test_saved_note_round_trips_through_the_parser() -> None:
    text = render_review_note("### Meetings\nnone\n", now=NOW, monday=THIS_MONDAY)
    body, at = parse_review_note(text)
    assert body == "### Meetings\nnone"
    assert at == "2026-07-29 17:30"


def test_parse_tolerates_a_hand_edited_note() -> None:
    body, at = parse_review_note("# Weekly Review — 2026-W31\n\nI rewrote this myself\n")
    assert body == "I rewrote this myself"
    assert at is None
