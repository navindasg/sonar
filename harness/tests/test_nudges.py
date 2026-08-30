"""nudges — the SILENT "what wants my attention" engine.

The nudge surface is the safe replacement for proactive audio: it never speaks,
never pushes, never notifies — the caller polls it. So these tests pin the two
properties that make it safe to poll and safe to trust:

  * **Deterministic ranking.** The same facts always produce the same ordered
    list, with a total order (severity -> source -> rank -> id) and no ties left
    to dict/thread ordering.
  * **Graceful absence.** A source that raises, stalls, or is simply not
    connected costs its own nudge and nothing else — never an exception and
    never an error payload.

The line-parsers are deliberately tested against the REAL renderers
(``calendar_read.render_events`` / ``gmail_read.render_messages``) rather than
hand-written strings: nudges compose those tools' output, so a drift in their
format must fail HERE rather than silently emptying the surface in production.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sonar_harness import nudges as nd
from sonar_harness.nudges import (
    HIGH,
    MEDIUM,
    Nudge,
    NudgeEngine,
    collect,
    email_nudges,
    meeting_nudges,
    notes_nudges,
    parse_agenda,
    parse_messages,
    read_notes_marks,
    sort_nudges,
    todo_nudges,
)
from sonar_harness.tools.base import ToolContext
from sonar_harness.tools.calendar_read import render_events
from sonar_harness.tools.gmail_read import render_messages
from sonar_harness.tools.todo_list import TodoListTool

EAST = timezone(timedelta(hours=-4))
NOW = datetime(2026, 7, 26, 14, 0, tzinfo=EAST)


def _ctx() -> ToolContext:
    return ToolContext(turn_id="t", state=None, emit=lambda _e: None)


def _n(nid: str, *, source: str = "todo", severity: str = MEDIUM, rank: int = 0) -> Nudge:
    return Nudge(id=nid, source=source, severity=severity, rank=rank, line=nid)


# ---- Nudge shape + ranking ---------------------------------------------------
def test_nudge_dict_carries_the_fields_the_surface_renders() -> None:
    keys = set(_n("x").to_dict())
    assert keys == {"id", "source", "severity", "rank", "line", "at"}


def test_severity_outranks_source() -> None:
    low_prio_source_but_high = _n("a", source="todo", severity=HIGH)
    top_source_but_medium = _n("b", source="calendar", severity=MEDIUM)
    ordered = sort_nudges([top_source_but_medium, low_prio_source_but_high])
    assert [n.id for n in ordered] == ["a", "b"]


def test_source_breaks_a_severity_tie_in_a_fixed_order() -> None:
    given = [
        _n("t", source="todo", severity=HIGH),
        _n("e", source="email", severity=HIGH),
        _n("n", source="notes", severity=HIGH),
        _n("c", source="calendar", severity=HIGH),
    ]
    assert [n.id for n in sort_nudges(given)] == ["c", "n", "e", "t"]


def test_rank_orders_within_a_source_lowest_first() -> None:
    given = [_n("far", rank=30), _n("near", rank=2), _n("mid", rank=10)]
    assert [n.id for n in sort_nudges(given)] == ["near", "mid", "far"]


def test_id_is_the_final_tiebreak_so_the_order_is_total() -> None:
    given = [_n("zzz"), _n("aaa"), _n("mmm")]
    assert [n.id for n in sort_nudges(given)] == ["aaa", "mmm", "zzz"]


def test_sort_returns_a_new_tuple_and_leaves_the_input_alone() -> None:
    given = [_n("b", rank=2), _n("a", rank=1)]
    out = sort_nudges(given)
    assert isinstance(out, tuple)
    assert [n.id for n in given] == ["b", "a"]  # caller's list untouched


def test_unknown_severity_or_source_sorts_last_instead_of_crashing() -> None:
    weird = Nudge(id="w", source="mystery", severity="whatever", rank=0, line="w")
    ordered = sort_nudges([weird, _n("a", source="calendar", severity=HIGH)])
    assert [n.id for n in ordered] == ["a", "w"]


# ---- agenda parsing (pinned to the real renderer) ----------------------------
def test_parse_agenda_reads_the_real_render_events_output() -> None:
    text = render_events(
        [
            {
                "id": "abc123",
                "start": {"dateTime": "2026-07-26T14:30:00-04:00"},
                "summary": "Standup",
                "location": "Zoom",
            }
        ]
    )
    (start, label, event_id), = parse_agenda(text)
    assert start == datetime(2026, 7, 26, 14, 30, tzinfo=EAST)
    assert "Standup" in label
    assert event_id == "abc123"
    assert "[id:" not in label  # the id tag is stripped from the human label


def test_parse_agenda_skips_all_day_events() -> None:
    """A date-only event has no start TIME, so "starts in N minutes" is
    meaningless for it — and treating it as local midnight would fire a bogus
    nudge just before midnight."""
    text = render_events([{"start": {"date": "2026-07-27"}, "summary": "Holiday"}])
    assert parse_agenda(text) == ()


def test_parse_agenda_handles_the_empty_and_error_strings() -> None:
    assert parse_agenda(render_events([])) == ()
    assert parse_agenda("Google is not connected yet. Run `scripts/sonar.sh`.") == ()
    assert parse_agenda("") == ()


def test_parse_agenda_sorts_by_start_time() -> None:
    text = render_events(
        [
            {"start": {"dateTime": "2026-07-26T16:00:00-04:00"}, "summary": "Later"},
            {"start": {"dateTime": "2026-07-26T15:00:00-04:00"}, "summary": "Sooner"},
        ]
    )
    assert [label for _s, label, _i in parse_agenda(text)] == ["Sooner", "Later"]


# ---- meeting nudge -----------------------------------------------------------
def _agenda(minutes_from_now: int, summary: str = "Standup", eid: str = "e1") -> str:
    start = NOW + timedelta(minutes=minutes_from_now)
    return render_events([{"id": eid, "start": {"dateTime": start.isoformat()}, "summary": summary}])


def test_meeting_inside_the_window_nudges() -> None:
    (nudge,) = meeting_nudges(_agenda(12), NOW)
    assert nudge.source == "calendar"
    assert nudge.severity == MEDIUM
    assert nudge.rank == 12
    assert "Standup" in nudge.line and "12" in nudge.line
    assert nudge.at.startswith("2026-07-26T14:12")


def test_meeting_beyond_the_window_is_silent() -> None:
    assert meeting_nudges(_agenda(90), NOW) == ()


def test_imminent_meeting_is_high_severity() -> None:
    (nudge,) = meeting_nudges(_agenda(3), NOW)
    assert nudge.severity == HIGH


def test_meeting_window_and_urgency_come_from_env(monkeypatch) -> None:
    monkeypatch.setenv(nd.ENV_MEETING_MINUTES, "120")
    monkeypatch.setenv(nd.ENV_MEETING_URGENT_MINUTES, "100")
    (nudge,) = meeting_nudges(_agenda(90), NOW)
    assert nudge.severity == HIGH


def test_only_the_next_meeting_nudges() -> None:
    text = render_events(
        [
            {"id": "a", "start": {"dateTime": (NOW + timedelta(minutes=20)).isoformat()}, "summary": "Later"},
            {"id": "b", "start": {"dateTime": (NOW + timedelta(minutes=5)).isoformat()}, "summary": "Sooner"},
        ]
    )
    (nudge,) = meeting_nudges(text, NOW)
    assert "Sooner" in nudge.line
    assert nudge.id == "meeting:b"  # id comes from the calendar event, so it is stable


def test_meeting_already_underway_is_not_a_nudge() -> None:
    assert meeting_nudges(_agenda(-10), NOW) == ()


def test_meeting_nudge_id_is_stable_across_polls() -> None:
    first = meeting_nudges(_agenda(12), NOW)[0]
    second = meeting_nudges(_agenda(11), NOW + timedelta(minutes=1))[0]
    assert first.id == second.id  # same meeting, one minute later


# ---- email parsing + nudge ---------------------------------------------------
def _message(subject: str, *, hours_ago: float) -> dict:
    when = NOW - timedelta(hours=hours_ago)
    return {
        "snippet": "…",
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": when.strftime("%a, %d %b %Y %H:%M:%S %z")},
            ]
        },
    }


def test_parse_messages_reads_the_real_render_messages_output() -> None:
    text = render_messages([_message("Contract review", hours_ago=5)])
    (subject, received), = parse_messages(text)
    assert subject == "Contract review"
    assert received == NOW - timedelta(hours=5)


def test_parse_messages_handles_the_empty_and_error_strings() -> None:
    assert parse_messages(render_messages([])) == ()
    assert parse_messages("Google is not connected yet.") == ()


def test_parse_messages_reads_a_date_header_with_a_timezone_comment() -> None:
    """Real Gmail Date headers usually carry an RFC-2822 comment — "… -0400
    (EDT)". A parser that stopped at the first ")" dropped every such message,
    i.e. most of a real inbox, and the surface looked like an empty one."""
    text = render_messages(
        [
            {
                "snippet": "x",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "alice@example.com"},
                        {"name": "Subject", "value": "Contract review"},
                        {"name": "Date", "value": "Sun, 26 Jul 2026 04:00:00 -0400 (EDT)"},
                    ]
                },
            }
        ]
    )
    (subject, received), = parse_messages(text)
    assert subject == "Contract review"
    assert received == NOW - timedelta(hours=10)


def test_parse_messages_keeps_an_em_dash_inside_the_subject() -> None:
    """Newsletter subjects are full of em dashes; splitting at the FIRST one
    truncated "Q3 — final numbers" to a useless "Q3"."""
    text = render_messages([_message("Q3 — final numbers", hours_ago=5)])
    (subject, _received), = parse_messages(text)
    assert subject == "Q3 — final numbers"


def test_parse_messages_keeps_parentheses_inside_the_subject() -> None:
    text = render_messages([_message("Invoice (draft) ready", hours_ago=5)])
    (subject, _received), = parse_messages(text)
    assert subject == "Invoice (draft) ready"


def test_parse_messages_skips_an_unparseable_date() -> None:
    text = render_messages(
        [
            {
                "snippet": "x",
                "payload": {"headers": [{"name": "Subject", "value": "S"},
                                        {"name": "Date", "value": "sometime"}]},
            }
        ]
    )
    assert parse_messages(text) == ()


def test_old_unread_important_mail_nudges() -> None:
    text = render_messages([_message("Contract review", hours_ago=9)])
    (nudge,) = email_nudges(text, NOW)
    assert nudge.source == "email"
    assert nudge.severity == MEDIUM
    assert "Contract review" in nudge.line
    assert nudge.id == "email:unread-important"  # aggregate, so it is stable


def test_recent_unread_mail_is_silent() -> None:
    """Below the age threshold it is just today's inbox, not a nudge."""
    assert email_nudges(render_messages([_message("Just landed", hours_ago=0.5)]), NOW) == ()


def test_email_age_threshold_comes_from_env(monkeypatch) -> None:
    text = render_messages([_message("Contract review", hours_ago=2)])
    assert email_nudges(text, NOW) == ()
    monkeypatch.setenv(nd.ENV_EMAIL_HOURS, "1")
    assert len(email_nudges(text, NOW)) == 1


def test_email_nudge_counts_the_pile_and_names_the_oldest() -> None:
    text = render_messages(
        [_message("Newer thing", hours_ago=4), _message("Oldest thing", hours_ago=30)]
    )
    (nudge,) = email_nudges(text, NOW)
    assert "2" in nudge.line
    assert "Oldest thing" in nudge.line
    assert nudge.at.startswith("2026-07-25")  # the oldest message's timestamp


# ---- todo nudge --------------------------------------------------------------
def _overdue_text(vault: Path) -> str:
    """Real TodoListTool output over a real vault — the source nudges compose."""
    return TodoListTool(vault_path=vault, today=NOW.date()).run(
        {"due": "overdue", "source": "user"}, _ctx()
    )


def test_todo_overdue_past_the_threshold_nudges(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("- [ ] file taxes 📅 2026-07-20\n", encoding="utf-8")
    (nudge,) = todo_nudges(_overdue_text(tmp_path), NOW)
    assert nudge.source == "todo"
    assert "file taxes" in nudge.line
    assert "6" in nudge.line  # six days overdue
    assert nudge.at == "2026-07-20"


def test_todo_barely_overdue_is_silent(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("- [ ] water plants 📅 2026-07-25\n", encoding="utf-8")
    assert todo_nudges(_overdue_text(tmp_path), NOW) == ()


def test_todo_days_threshold_comes_from_env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "notes.md").write_text("- [ ] water plants 📅 2026-07-25\n", encoding="utf-8")
    monkeypatch.setenv(nd.ENV_TODO_DAYS, "0")
    assert len(todo_nudges(_overdue_text(tmp_path), NOW)) == 1


def test_long_overdue_todo_is_high_severity(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("- [ ] renew passport 📅 2026-06-01\n", encoding="utf-8")
    (nudge,) = todo_nudges(_overdue_text(tmp_path), NOW)
    assert nudge.severity == HIGH


def test_todos_are_capped_and_the_most_overdue_come_first(tmp_path: Path, monkeypatch) -> None:
    lines = [f"- [ ] task {i} 📅 2026-07-{i:02d}\n" for i in range(1, 16)]
    (tmp_path / "notes.md").write_text("".join(lines), encoding="utf-8")
    monkeypatch.setenv(nd.ENV_TODO_MAX, "3")
    out = todo_nudges(_overdue_text(tmp_path), NOW)
    assert len(out) == 3
    assert "task 1" in out[0].line  # oldest due date first
    assert [n.rank for n in out] == sorted(n.rank for n in out)


def test_todo_id_survives_the_task_moving_down_the_note(tmp_path: Path) -> None:
    """Ids key off note + task text, not the line number, so an edit elsewhere in
    the note doesn't make an old nudge look brand new."""
    note = tmp_path / "notes.md"
    note.write_text("- [ ] file taxes 📅 2026-07-20\n", encoding="utf-8")
    before = todo_nudges(_overdue_text(tmp_path), NOW)[0]
    note.write_text("# heading\n\nsome prose\n\n- [ ] file taxes 📅 2026-07-20\n", encoding="utf-8")
    after = todo_nudges(_overdue_text(tmp_path), NOW)[0]
    assert before.id == after.id


def test_todo_nudges_survive_the_empty_and_error_strings() -> None:
    assert todo_nudges("No open to-do checkboxes found for due=overdue.", NOW) == ()
    assert todo_nudges("error: could not read the vault (OSError: nope).", NOW) == ()
    assert todo_nudges(json.dumps({"todos": "not-a-list"}), NOW) == ()


# ---- unsaved notes session ---------------------------------------------------
def test_unsaved_session_nudges_and_is_high_severity() -> None:
    started = NOW - timedelta(minutes=45)
    (nudge,) = notes_nudges(started, None, NOW)
    assert nudge.source == "notes"
    assert nudge.severity == HIGH  # an unsaved transcript is the one lossy case
    assert "13:15" in nudge.line


def test_session_saved_after_it_started_is_silent() -> None:
    started = NOW - timedelta(minutes=45)
    assert notes_nudges(started, started + timedelta(minutes=5), NOW) == ()


def test_a_session_still_in_progress_is_silent() -> None:
    """Below the grace window the meeting is probably still happening — nudging
    "you didn't save" mid-session would be noise."""
    assert notes_nudges(NOW - timedelta(minutes=2), None, NOW) == ()


def test_an_ancient_marker_stops_nudging() -> None:
    """A DISCARDED session leaves the same marker as an abandoned one, so the
    nudge expires rather than nagging forever."""
    assert notes_nudges(NOW - timedelta(days=4), None, NOW) == ()


def test_no_session_marker_is_silent() -> None:
    assert notes_nudges(None, None, NOW) == ()


def test_notes_grace_and_expiry_come_from_env(monkeypatch) -> None:
    monkeypatch.setenv(nd.ENV_NOTES_MINUTES, "1")
    assert len(notes_nudges(NOW - timedelta(minutes=2), None, NOW)) == 1
    monkeypatch.setenv(nd.ENV_NOTES_MAX_AGE_H, "200")
    assert len(notes_nudges(NOW - timedelta(days=4), None, NOW)) == 1


def test_read_notes_marks_compares_the_url_marker_to_the_saved_notes(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / "run").mkdir(parents=True)
    marker = home / "run" / "notes.url"
    marker.write_text("http://127.0.0.1:8771\n", encoding="utf-8")
    monkeypatch.setenv("SONAR_HOME", str(home))

    vault = tmp_path / "vault"
    saved = vault / "Sonar" / "Notes"
    saved.mkdir(parents=True)
    note = saved / "Kickoff.md"
    note.write_text("# Kickoff\n", encoding="utf-8")

    started, last_saved = read_notes_marks(vault)
    assert started is not None and last_saved is not None
    assert abs((last_saved - started).total_seconds()) < 60


def test_read_notes_marks_is_all_none_when_nothing_exists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SONAR_HOME", str(tmp_path / "missing"))
    assert read_notes_marks(tmp_path / "no-vault") == (None, None)


# ---- collect: isolation, deadlines, cap --------------------------------------
OK = Nudge(id="ok", source="todo", severity=MEDIUM, rank=0, line="ok")


def test_a_failing_source_costs_only_its_own_nudge() -> None:
    def boom(_now):
        raise RuntimeError("gmail exploded")

    out = collect((("email", boom), ("todo", lambda _now: [OK])), NOW, timeout_s=1.0)
    assert [n.id for n in out] == ["ok"]


def test_a_stalled_source_is_dropped_without_holding_up_the_poll() -> None:
    def slow(_now):
        time.sleep(5.0)
        return [OK]

    started = time.monotonic()
    out = collect((("email", slow), ("todo", lambda _now: [OK])), NOW, timeout_s=0.1)
    elapsed = time.monotonic() - started
    assert [n.id for n in out] == ["ok"]
    assert elapsed < 2.0  # the stalled source never blocked the answer


def test_collect_applies_the_deterministic_order_and_the_cap(monkeypatch) -> None:
    many = [
        Nudge(id=f"n{i}", source="todo", severity=MEDIUM, rank=-i, line=f"n{i}")
        for i in range(6)
    ]
    monkeypatch.setenv(nd.ENV_MAX, "2")
    out = collect((("todo", lambda _now: many),), NOW, timeout_s=1.0)
    assert [n.id for n in out] == ["n5", "n4"]


def test_collect_with_no_sources_is_empty_not_an_error() -> None:
    assert collect((), NOW, timeout_s=1.0) == ()


# ---- engine: snapshot shape + TTL cache --------------------------------------
def _engine(sources, *, now: datetime = NOW) -> NudgeEngine:
    return NudgeEngine(vault_path="/nonexistent", sources=sources, clock=lambda: now)


def test_snapshot_shape_is_what_the_surface_renders() -> None:
    snap = _engine((("todo", lambda _now: [OK]),)).snapshot()
    assert set(snap) == {"generated_at", "age_s", "ttl_s", "count", "nudges"}
    assert snap["count"] == 1
    assert snap["nudges"][0]["id"] == "ok"
    assert snap["generated_at"].startswith("2026-07-26T14:00")


def test_a_poll_inside_the_ttl_does_not_rerun_the_sources(monkeypatch) -> None:
    calls: list[int] = []

    def counted(_now):
        calls.append(1)
        return [OK]

    monkeypatch.setenv(nd.ENV_TTL_S, "600")
    engine = _engine((("todo", counted),))
    engine.snapshot()
    engine.snapshot()
    engine.snapshot()
    assert len(calls) == 1  # polling is free between refreshes


def test_the_cache_expires_and_recomputes(monkeypatch) -> None:
    calls: list[int] = []

    def counted(_now):
        calls.append(1)
        return [OK]

    monkeypatch.setenv(nd.ENV_TTL_S, "0")
    engine = _engine((("todo", counted),))
    engine.snapshot()
    engine.snapshot()
    assert len(calls) == 2


def test_snapshot_never_raises_even_when_every_source_fails() -> None:
    def boom(_now):
        raise RuntimeError("everything is down")

    snap = _engine((("email", boom), ("todo", boom))).snapshot()
    assert snap["nudges"] == [] and snap["count"] == 0


def test_a_naive_clock_never_crashes_a_source(tmp_path: Path) -> None:
    """Parsed timestamps are always aware, so a naive ``now`` used to raise
    TypeError deep inside a source. ``snapshot`` promises never to raise, and a
    silently-empty surface is exactly the failure this module exists to avoid —
    so the sources normalise the clock instead of blowing up."""
    naive = NOW.replace(tzinfo=None)
    (meeting,) = meeting_nudges(_agenda(12), naive)
    assert meeting.rank == 12
    (mail,) = email_nudges(render_messages([_message("Old thing", hours_ago=9)]), naive)
    assert "Old thing" in mail.line
    (note,) = notes_nudges(NOW - timedelta(minutes=45), None, naive)
    assert note.severity == HIGH


# ---- default_sources end to end (real data, not just the degraded path) ------
class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def execute(self) -> dict:
        return self._payload


class _FakeCalendar:
    """Enough of the Calendar client for ``CalendarAgendaTool.run``."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def events(self) -> "_FakeCalendar":
        return self

    def list(self, **_kw) -> _FakeRequest:
        return _FakeRequest({"items": self._items})


class _FakeGmail:
    """Enough of the Gmail client for ``GmailSearchTool.run``."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages

    def users(self) -> "_FakeGmail":
        return self

    def messages(self) -> "_FakeGmail":
        return self

    def list(self, **_kw) -> _FakeRequest:
        return _FakeRequest({"messages": [{"id": str(i)} for i, _ in enumerate(self._messages)]})

    def get(self, *, id: str, **_kw) -> _FakeRequest:  # noqa: A002 - mirrors the API kwarg
        return _FakeRequest(self._messages[int(id)])


def test_default_sources_turn_real_tool_output_into_nudges(tmp_path: Path, monkeypatch) -> None:
    """The wiring, not just the parsers: every source is driven with data that
    SHOULD nudge, so a wrong tool argument or a swapped parser fails here. The
    "nothing is connected" test below only ever exercises the empty path."""
    monkeypatch.setattr(
        "sonar_harness.tools.calendar_read.build_service",
        lambda *_a, **_kw: _FakeCalendar(
            [{"id": "e1", "start": {"dateTime": (NOW + timedelta(minutes=9)).isoformat()},
              "summary": "Standup"}]
        ),
    )
    monkeypatch.setattr(
        "sonar_harness.tools.gmail_read.build_service",
        lambda *_a, **_kw: _FakeGmail([_message("Contract review", hours_ago=9)]),
    )
    (tmp_path / "notes.md").write_text("- [ ] file taxes 📅 2026-07-20\n", encoding="utf-8")
    monkeypatch.setenv("SONAR_HOME", str(tmp_path / "home"))

    sources = dict(nd.default_sources(vault_path=tmp_path))
    assert [n.line for n in sources["calendar"](NOW)] == ["Standup starts in 9 min"]
    assert "Contract review" in sources["email"](NOW)[0].line
    assert "file taxes" in sources["todo"](NOW)[0].line
    assert sources["notes"](NOW) == ()  # no session marker on this machine


def test_default_sources_are_exactly_the_four_pull_only_reads(tmp_path: Path) -> None:
    """Guard rail: nudges are PULL-only. Nothing here may talk to the voice
    socket, speak, or push — the source list is the whole surface area."""
    assert [name for name, _fn in nd.default_sources(vault_path=tmp_path)] == [
        "calendar",
        "email",
        "todo",
        "notes",
    ]


def test_the_real_engine_degrades_to_empty_when_nothing_is_connected(
    tmp_path: Path, monkeypatch
) -> None:
    """End to end with the real sources and no Google, no notes, an empty vault:
    an empty list, never an exception."""
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/google_token.json")
    monkeypatch.setenv("SONAR_HOME", str(tmp_path / "home"))
    snap = NudgeEngine(vault_path=tmp_path).snapshot()
    assert snap["nudges"] == []


# ---- the /nudges route -------------------------------------------------------
def test_nudges_is_registered_as_a_read_only_get() -> None:
    """The tests below call the handler directly (no TestClient, so no lifespan
    and no Ollama preload), which means nothing else would catch a typo in the
    decorator. Pin the path AND that it is GET-only: a poll must never be able
    to mutate anything."""
    from sonar_harness.server import app

    routes = {r.path: sorted(r.methods) for r in app.routes if getattr(r, "methods", None)}
    assert routes["/nudges"] == ["GET"]


def test_nudges_route_returns_a_json_body_with_the_snapshot(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import types

    from sonar_harness.server import get_nudges

    monkeypatch.setenv("SONAR_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/google_token.json")
    monkeypatch.setenv("SONAR_HOME", str(tmp_path / "home"))

    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
    response = asyncio.run(get_nudges(request))
    assert response.status_code == 200
    body = json.loads(bytes(response.body))
    assert body["nudges"] == [] and body["count"] == 0


def test_nudges_route_reuses_one_engine_so_polling_stays_cheap(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    import types

    from sonar_harness.server import get_nudges

    monkeypatch.setenv("SONAR_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/google_token.json")
    monkeypatch.setenv("SONAR_HOME", str(tmp_path / "home"))

    app = types.SimpleNamespace(state=types.SimpleNamespace())
    request = types.SimpleNamespace(app=app)
    asyncio.run(get_nudges(request))
    first = app.state.nudges
    asyncio.run(get_nudges(request))
    assert app.state.nudges is first


def test_nudges_route_returns_an_empty_list_rather_than_an_error_page(monkeypatch) -> None:
    """A polled surface must never render a 500 in the user's menu bar."""
    import asyncio
    import types

    from sonar_harness import server

    class _Broken:
        def snapshot(self):
            raise RuntimeError("engine is wedged")

    app = types.SimpleNamespace(state=types.SimpleNamespace(nudges=_Broken()))
    response = asyncio.run(server.get_nudges(types.SimpleNamespace(app=app)))
    assert response.status_code == 200
    assert json.loads(bytes(response.body))["nudges"] == []
