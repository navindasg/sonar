"""Vault save: rendered markdown, slug safety, collisions, atomic re-save."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from notes import session as sess
from notes.store import pick_path, render_note, save_note, slug_for

NOW = datetime(2026, 7, 15, 14, 30)


def _state(title: str = "Budget Review") -> sess.SessionState:
    s = sess.SessionState(title=title, started_at="2026-07-15T14:00:00")
    s = sess.add_segment(s, "S1", "let's start with the numbers", 0.0, 2.5)
    s = sess.add_segment(s, "S2", "revenue is up eight percent", 65.0, 68.0)
    s = sess.rename_speaker(s, "S1", "Navin")
    s = sess.set_summary(s, "### Summary\n\n- numbers reviewed")
    return s


def test_render_has_overview_then_transcript() -> None:
    md = render_note(_state(), NOW)
    assert md.index("## AI Overview") < md.index("- numbers reviewed") < md.index("## Transcript")
    assert "**Navin** (00:00): let's start with the numbers" in md
    assert "**Speaker 2** (01:05): revenue is up eight percent" in md
    assert "speakers: [Navin, Speaker 2]" in md
    assert "# Budget Review" in md


def test_render_without_summary_marks_it() -> None:
    s = sess.set_summary(_state(), "")
    assert "_(no AI overview)_" in render_note(s, NOW)


@pytest.mark.parametrize("title,expect", [
    ("../../etc/passwd", "etc passwd"),          # separators and dots stripped
    ("Budget: Q3 review!", "Budget Q3 review"),
    ("   ", "FALLBACK"),
    ("", "FALLBACK"),
])
def test_slug_never_escapes(title: str, expect: str) -> None:
    assert slug_for(title, fallback="FALLBACK") == expect


def test_save_writes_under_sonar_notes(tmp_path: Path) -> None:
    target = save_note(_state(), tmp_path, NOW)
    assert target == tmp_path / "Sonar" / "Notes" / "Budget Review.md"
    text = target.read_text(encoding="utf-8")
    assert text.startswith("---\ncreated: 2026-07-15 14:30")
    assert not list(target.parent.glob("*.tmp"))  # atomic swap left no temp file


def test_collision_gets_a_numbered_suffix(tmp_path: Path) -> None:
    save_note(_state(), tmp_path, NOW)
    second = pick_path(tmp_path, _state(), NOW)
    assert second.name == "Budget Review-2.md"


def test_resave_overwrites_the_same_file(tmp_path: Path) -> None:
    first = save_note(_state(), tmp_path, NOW)
    edited = sess.set_summary(_state(), "### Summary\n\n- EDITED")
    again = save_note(edited, tmp_path, NOW, path=first)
    assert again == first
    assert "- EDITED" in first.read_text(encoding="utf-8")
    assert len(list(first.parent.iterdir())) == 1   # no duplicate note


def test_missing_vault_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        save_note(_state(), tmp_path / "nope", NOW)
