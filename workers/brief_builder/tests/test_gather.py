"""Tests for gathering recent notes from a temp vault."""

from __future__ import annotations

from pathlib import Path

from brief_builder.gather import gather_recent_notes

from conftest import write_note


def test_returns_empty_for_missing_vault(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert gather_recent_notes(missing, max_notes=8, max_chars_per_note=600) == ()


def test_orders_by_mtime_desc_and_respects_max(temp_vault: Path) -> None:
    write_note(temp_vault, "old.md", "# Old\nold body", mtime=1000)
    write_note(temp_vault, "mid.md", "# Mid\nmid body", mtime=2000)
    write_note(temp_vault, "new.md", "# New\nnew body", mtime=3000)

    notes = gather_recent_notes(temp_vault, max_notes=2, max_chars_per_note=600)
    titles = [n.title for n in notes]
    assert titles == ["New", "Mid"]


def test_excludes_sonar_output_folder(temp_vault: Path) -> None:
    write_note(temp_vault, "keep.md", "# Keep\nkeep me", mtime=1000)
    # These live under the Sonar/ output tree and must be excluded.
    write_note(
        temp_vault, "Sonar/Briefs/2026-07-06-any.md", "# Brief\nself", mtime=5000
    )
    write_note(temp_vault, "Sonar/other.md", "# Other\nx", mtime=6000)

    notes = gather_recent_notes(temp_vault, max_notes=8, max_chars_per_note=600)
    titles = {n.title for n in notes}
    assert titles == {"Keep"}


def test_title_falls_back_to_stem_and_excerpt_skips_headings(
    temp_vault: Path,
) -> None:
    write_note(
        temp_vault,
        "no-h1.md",
        "## subheading\n\nfirst real line here\nsecond line",
        mtime=1000,
    )
    (note,) = gather_recent_notes(temp_vault, max_notes=8, max_chars_per_note=600)
    assert note.title == "no-h1"
    assert note.excerpt == "first real line here"


def test_excerpt_is_truncated(temp_vault: Path) -> None:
    long_line = "x" * 1000
    write_note(temp_vault, "long.md", f"# Long\n{long_line}", mtime=1000)
    (note,) = gather_recent_notes(temp_vault, max_notes=8, max_chars_per_note=50)
    assert len(note.excerpt) == 50
    assert note.excerpt.endswith("…")
