"""todo_list: deterministic vault scan for OPEN '- [ ]' checkboxes, with date
parsing (Tasks-plugin / daily-note), sonar-vs-user source, and due filtering."""

from __future__ import annotations

import json
from datetime import date

from sonar_harness.tools.base import ToolContext
from sonar_harness.tools.todo_list import TodoListTool

TODAY = date(2026, 7, 8)


def _ctx():
    events: list[dict] = []
    return ToolContext(turn_id="t", state=None, emit=events.append), events


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _tool(vault):
    return TodoListTool(vault_path=vault, today=TODAY)


def test_collects_open_skips_done_and_reports_source(tmp_path):
    _write(tmp_path, "projects/home.md", "notes\n  - [ ] hang the shelf\n* [ ] sort the garage\n- [x] done\n")
    out = json.loads(_tool(tmp_path).run({}, _ctx()[0]))
    tasks = {t["task"] for t in out["todos"]}
    assert tasks == {"hang the shelf", "sort the garage"}  # "[x]" excluded
    assert out["count"] == 2 and out["truncated"] is False
    assert all(t["source"] == "user" for t in out["todos"])


def test_source_distinguishes_sonar_generated_notes(tmp_path):
    _write(tmp_path, "Sonar/Briefs/2026-07-06-any.md", "- [ ] follow up on the brief\n")
    _write(tmp_path, "errands.md", "- [ ] buy stamps\n")
    out = json.loads(_tool(tmp_path).run({}, _ctx()[0]))
    by_task = {t["task"]: t["source"] for t in out["todos"]}
    assert by_task == {"follow up on the brief": "sonar", "buy stamps": "user"}
    # filter to just the user's own todos
    only_user = json.loads(_tool(tmp_path).run({"source": "user"}, _ctx()[0]))
    assert [t["task"] for t in only_user["todos"]] == ["buy stamps"]


def test_dates_from_tasks_marker_and_daily_note_filename(tmp_path):
    _write(tmp_path, "2026-07-08.md", "- [ ] item in today's daily note\n")           # date from filename
    _write(tmp_path, "inbox.md", "- [ ] pay invoice 📅 2026-07-01\n- [ ] someday\n")   # due marker + undated
    out = json.loads(_tool(tmp_path).run({}, _ctx()[0]))
    dates = {t["task"]: t["date"] for t in out["todos"]}
    assert dates["item in today's daily note"] == "2026-07-08"
    assert dates["pay invoice 📅 2026-07-01"] == "2026-07-01"
    assert dates["someday"] is None
    # dated sort ascending, undated last
    assert [t["date"] for t in out["todos"]] == ["2026-07-01", "2026-07-08", None]


def test_due_filters_today_overdue_undated(tmp_path):
    _write(tmp_path, "2026-07-08.md", "- [ ] due today\n")
    _write(tmp_path, "inbox.md", "- [ ] overdue thing 📅 2026-07-01\n- [ ] no date\n")
    run = lambda a: json.loads(_tool(tmp_path).run(a, _ctx()[0]))  # noqa: E731
    assert [t["task"] for t in run({"due": "today"})["todos"]] == ["due today"]
    assert [t["task"] for t in run({"due": "overdue"})["todos"]] == ["overdue thing 📅 2026-07-01"]
    assert [t["task"] for t in run({"due": "undated"})["todos"]] == ["no date"]
    assert run({"due": "today"})["today"] == "2026-07-08"


def test_empty_and_no_match_messages(tmp_path):
    _write(tmp_path, "note.md", "just prose\n- a plain bullet\n")
    assert _tool(tmp_path).run({}, _ctx()[0]) == "No open to-do checkboxes found."
    _write(tmp_path, "b.md", "- [ ] undated task\n")
    msg = _tool(tmp_path).run({"due": "today"}, _ctx()[0])
    assert "due=today" in msg


def test_skips_obsidian_machinery(tmp_path):
    _write(tmp_path, ".obsidian/plugins/x.md", "- [ ] not a real todo\n")
    _write(tmp_path, "real.md", "- [ ] a real todo\n")
    out = json.loads(_tool(tmp_path).run({}, _ctx()[0]))
    assert [t["task"] for t in out["todos"]] == ["a real todo"]


def test_invalid_filter_is_rejected(tmp_path):
    _write(tmp_path, "n.md", "- [ ] x\n")
    assert _tool(tmp_path).run({"due": "yesterday"}, _ctx()[0]).startswith("error:")
