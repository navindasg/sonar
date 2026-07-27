"""daily.brief — read-back-first semantics, composition, and the saved artifact.

The brief is PULL-only (nothing schedules it any more), so the tool itself owns
the artifact: the first ask of the day composes and saves it, every later ask
reads that note straight back. These tests pin both halves plus the refresh
escape hatch.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from sonar_harness.tools import daily_brief as db
from sonar_harness.tools.base import ToolContext
from sonar_harness.tools.daily_brief import DailyBriefTool, brief_path, render_brief

TODAY = datetime(2026, 7, 26, 9, 12)


def _ctx(events: list[dict] | None = None) -> ToolContext:
    sink = events if events is not None else []
    return ToolContext(turn_id="t", state=None, emit=sink.append)


def _tool(vault: Path, *, now: datetime = TODAY, monkeypatch: pytest.MonkeyPatch) -> DailyBriefTool:
    """A tool pinned to a fixed 'now' and with no Google token (sources degrade)."""
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/google_token.json")
    monkeypatch.setattr(db, "_now", lambda: now)
    return DailyBriefTool(vault_path=str(vault))


# ---- pure helpers ------------------------------------------------------------
def test_render_brief_labels_each_section() -> None:
    out = render_brief([("Calendar", "nothing today"), ("Due today", "- [ ] x")])
    assert "### Calendar" in out and "nothing today" in out
    assert "### Due today" in out and "- [ ] x" in out


def test_brief_path_is_the_dated_vault_note(tmp_path: Path) -> None:
    assert brief_path(tmp_path, TODAY.date()) == (
        tmp_path / "Sonar" / "Brief" / "2026-07-26.md"
    )


# ---- compose + save (first ask of the day) -----------------------------------
def test_first_ask_composes_every_section(tmp_path: Path, monkeypatch) -> None:
    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    assert "Today's calendar" in out
    assert "Overdue to-dos" in out and "Due today" in out
    assert "Unread important email" in out  # email is part of the brief by design
    assert "google" in out.lower()          # calendar degrades to its auth hint


def test_first_ask_saves_the_note(tmp_path: Path, monkeypatch) -> None:
    _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    note = brief_path(tmp_path, TODAY.date())
    assert note.exists()
    body = note.read_text(encoding="utf-8")
    assert "2026-07-26" in body and "09:12" in body
    assert "Today's calendar" in body
    assert not list(note.parent.glob("*.tmp"))  # atomic write left no scratch file


def test_first_ask_surfaces_a_real_todo(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "2020-01-01.md").write_text("- [ ] pay rent\n", encoding="utf-8")
    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    assert "pay rent" in out


# ---- read back (every later ask) ---------------------------------------------
def test_later_ask_reads_the_saved_note_back(tmp_path: Path, monkeypatch) -> None:
    tool = _tool(tmp_path, monkeypatch=monkeypatch)
    tool.run({}, _ctx())                       # first ask writes it
    (tmp_path / "2020-01-01.md").write_text("- [ ] appeared later\n", encoding="utf-8")

    out = tool.run({}, _ctx())                 # second ask must NOT recompose
    assert "appeared later" not in out
    assert "Today's calendar" in out           # the saved content still comes back


def test_read_back_says_when_it_was_prepared(tmp_path: Path, monkeypatch) -> None:
    tool = _tool(tmp_path, monkeypatch=monkeypatch)
    tool.run({}, _ctx())
    out = tool.run({}, _ctx())
    assert "09:12" in out  # so the model can say "as of this morning"


def test_read_back_skips_the_source_tools(tmp_path: Path, monkeypatch) -> None:
    """The whole point of the cache: no calendar/todo/gmail calls on a re-ask."""
    tool = _tool(tmp_path, monkeypatch=monkeypatch)
    tool.run({}, _ctx())

    events: list[dict] = []
    tool.run({}, _ctx(events))
    tools_called = {e.get("tool") for e in events}
    assert "calendar.agenda" not in tools_called
    assert "gmail.search" not in tools_called


def test_yesterdays_note_does_not_count_as_todays(tmp_path: Path, monkeypatch) -> None:
    stale = brief_path(tmp_path, datetime(2026, 7, 25).date())
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("# Brief — 2026-07-25\n\nyesterday's news\n", encoding="utf-8")

    out = _tool(tmp_path, monkeypatch=monkeypatch).run({}, _ctx())
    assert "yesterday's news" not in out
    assert brief_path(tmp_path, TODAY.date()).exists()


# ---- refresh -----------------------------------------------------------------
def test_refresh_recomposes_and_overwrites(tmp_path: Path, monkeypatch) -> None:
    tool = _tool(tmp_path, monkeypatch=monkeypatch)
    tool.run({}, _ctx())
    (tmp_path / "2020-01-01.md").write_text("- [ ] added after\n", encoding="utf-8")

    out = tool.run({"refresh": True}, _ctx())
    assert "added after" in out
    assert "added after" in brief_path(tmp_path, TODAY.date()).read_text(encoding="utf-8")


def test_unreadable_note_falls_back_to_composing(tmp_path: Path, monkeypatch) -> None:
    """A corrupt/unreadable artifact must never cost the user their brief."""
    tool = _tool(tmp_path, monkeypatch=monkeypatch)
    note = brief_path(tmp_path, TODAY.date())
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes(b"\xff\xfe not valid utf-8")

    out = tool.run({}, _ctx())
    assert "Today's calendar" in out
