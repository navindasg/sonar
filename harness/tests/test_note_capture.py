"""note.capture — slug safety, entry rendering, day-heading merge, and file writes."""

from __future__ import annotations

from datetime import datetime

from sonar_harness.tools.base import ToolContext
from sonar_harness.tools.note_capture import (
    NoteCaptureTool,
    append_under_today,
    render_entry,
    slug_for,
)

NOW = datetime(2026, 7, 10, 14, 30)


def _ctx(events: list | None = None) -> ToolContext:
    sink = events.append if events is not None else (lambda e: None)
    return ToolContext(turn_id="t", state=None, emit=sink)


def test_slug_defaults_and_sanitizes() -> None:
    assert slug_for(None) == "Inbox"
    assert slug_for("   ") == "Inbox"
    assert slug_for("ideas") == "ideas"
    assert slug_for("a" * 200) == "a" * 60  # length-capped
    # path traversal / separators are stripped -> a single safe stem
    s = slug_for("../../etc/passwd")
    assert "/" not in s and ".." not in s and s != "Inbox"


def test_render_entry_task_vs_timestamped_note() -> None:
    assert render_entry("buy milk", True, NOW) == "- [ ] buy milk"
    assert render_entry("random thought", False, NOW) == "- 14:30 random thought"


def test_append_creates_reuses_and_rolls_the_day_heading() -> None:
    c1 = append_under_today("# Inbox\n", "- a", "2026-07-10")
    assert "## 2026-07-10" in c1 and c1.rstrip().endswith("- a")
    c2 = append_under_today(c1, "- b", "2026-07-10")   # same day -> no new heading
    assert c2.count("## 2026-07-10") == 1 and c2.rstrip().endswith("- b")
    c3 = append_under_today(c2, "- c", "2026-07-11")   # new day -> new heading
    assert c3.count("## 2026-07-11") == 1


def test_run_writes_note_to_inbox(tmp_path) -> None:
    events: list = []
    out = NoteCaptureTool(vault_path=tmp_path, now=NOW).run(
        {"text": "call the dentist"}, _ctx(events)
    )
    body = (tmp_path / "Sonar" / "Inbox.md").read_text()
    assert "# Inbox" in body
    assert "## 2026-07-10" in body
    assert "- 14:30 call the dentist" in body
    assert "Sonar/Inbox.md" in out
    assert events[0]["tool"] == "note.capture" and events[0]["status"] == "ok"


def test_run_target_and_as_task(tmp_path) -> None:
    NoteCaptureTool(vault_path=tmp_path, now=NOW).run(
        {"text": "milk", "target": "groceries", "as_task": True}, _ctx()
    )
    assert "- [ ] milk" in (tmp_path / "Sonar" / "groceries.md").read_text()


def test_run_target_traversal_stays_inside_sonar(tmp_path) -> None:
    NoteCaptureTool(vault_path=tmp_path, now=NOW).run(
        {"text": "x", "target": "../../evil"}, _ctx()
    )
    assert not (tmp_path.parent / "evil.md").exists()
    written = list((tmp_path / "Sonar").glob("*.md"))
    assert len(written) == 1  # exactly one file, inside Sonar/


def test_run_is_additive_never_overwrites(tmp_path) -> None:
    # A note that already has user content: appending must preserve every line.
    sonar = tmp_path / "Sonar"
    sonar.mkdir()
    (sonar / "ideas.md").write_text(
        "# ideas\n\n## 2026-07-09\n\n- pre-existing user line\n"
    )
    NoteCaptureTool(vault_path=tmp_path, now=NOW).run(
        {"text": "brand new idea", "target": "ideas"}, _ctx()
    )
    body = (sonar / "ideas.md").read_text()
    assert "- pre-existing user line" in body   # old content survives
    assert "brand new idea" in body             # new content added
    assert body.count("# ideas") == 1           # header not duplicated
    assert body.index("pre-existing") < body.index("brand new")  # appended after


def test_run_rejects_empty_text(tmp_path) -> None:
    out = NoteCaptureTool(vault_path=tmp_path, now=NOW).run({"text": "  "}, _ctx())
    assert out.startswith("error:")


def test_run_errors_on_missing_vault() -> None:
    out = NoteCaptureTool(vault_path="/nope/xyz", now=NOW).run({"text": "hi"}, _ctx())
    assert out.startswith("error:")
