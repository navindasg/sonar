"""daily.brief — section composition and graceful degradation with no Google."""

from __future__ import annotations

from sonar_harness.tools.base import ToolContext
from sonar_harness.tools.daily_brief import DailyBriefTool, render_brief


def _ctx() -> ToolContext:
    return ToolContext(turn_id="t", state=None, emit=lambda _e: None)


def test_render_brief_labels_each_section() -> None:
    out = render_brief([("Calendar", "nothing today"), ("Due today", "- [ ] x")])
    assert "### Calendar" in out and "nothing today" in out
    assert "### Due today" in out and "- [ ] x" in out


def test_brief_composes_calendar_and_todos(tmp_path, monkeypatch) -> None:
    # No Google token -> calendar degrades to its "run google-auth" hint, not a
    # crash; todos scan the (empty) tmp vault. The brief still assembles.
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/google_token.json")
    out = DailyBriefTool(vault_path=str(tmp_path)).run({}, _ctx())
    assert "Today's calendar" in out
    assert "Overdue to-dos" in out and "Due today" in out
    assert "google" in out.lower()          # calendar section shows the auth hint
    assert "email" not in out.lower()        # email is opt-in


def test_brief_include_email_adds_a_section(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/google_token.json")
    out = DailyBriefTool(vault_path=str(tmp_path)).run({"include_email": True}, _ctx())
    assert "Unread important email" in out


def test_brief_surfaces_a_real_todo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/google_token.json")
    # An overdue user checkbox in a dated daily note should land in the brief.
    (tmp_path / "2020-01-01.md").write_text("- [ ] pay rent\n")
    out = DailyBriefTool(vault_path=str(tmp_path)).run({}, _ctx())
    assert "pay rent" in out
