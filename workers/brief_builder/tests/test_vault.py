"""Tests for vault-write path safety — the CRITICAL invariant."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from brief_builder.vault import (
    UnsafeWritePathError,
    output_dir,
    target_path,
    write_note,
)


def test_target_path_is_inside_briefs_dir(temp_vault: Path) -> None:
    path = target_path(temp_vault, window="any", day="2026-07-06")
    assert path == output_dir(temp_vault) / "2026-07-06-any.md"
    assert output_dir(temp_vault) in path.parents


def test_write_creates_dirs_and_file(temp_vault: Path) -> None:
    path = target_path(temp_vault, window="morning", day="2026-07-06")
    written = write_note(temp_vault, path, "hello")
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "hello"
    assert written.parent == temp_vault / "Sonar" / "Briefs"


def test_preexisting_file_gets_timestamped_variant_not_clobbered(
    temp_vault: Path,
) -> None:
    now = datetime(2026, 7, 6, 8, 30, 15)
    first = target_path(temp_vault, window="any", day="2026-07-06", now=now)
    write_note(temp_vault, first, "ORIGINAL")

    # Same day+window again -> must NOT be the same path, must not clobber.
    second = target_path(temp_vault, window="any", day="2026-07-06", now=now)
    assert second != first
    assert second.name == "2026-07-06-any-083015.md"
    write_note(temp_vault, second, "SECOND")

    assert first.read_text(encoding="utf-8") == "ORIGINAL"
    assert second.read_text(encoding="utf-8") == "SECOND"


def test_write_note_refuses_to_overwrite(temp_vault: Path) -> None:
    path = target_path(temp_vault, window="any", day="2026-07-06")
    write_note(temp_vault, path, "first")
    with pytest.raises(FileExistsError):
        write_note(temp_vault, path, "second")


def test_write_note_rejects_path_outside_briefs(temp_vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "evil.md"
    with pytest.raises(UnsafeWritePathError):
        write_note(temp_vault, outside, "nope")
    assert not outside.exists()


def test_write_note_rejects_traversal_within_vault(temp_vault: Path) -> None:
    # A path that resolves back above the Briefs dir must be rejected.
    sneaky = output_dir(temp_vault) / ".." / ".." / "user-note.md"
    with pytest.raises(UnsafeWritePathError):
        write_note(temp_vault, sneaky, "nope")
    assert not (temp_vault / "user-note.md").exists()


def test_target_path_sanitizes_window_segment(temp_vault: Path) -> None:
    # Even a hostile window value stays inside the Briefs dir.
    path = target_path(temp_vault, window="../../etc", day="2026-07-06")
    assert output_dir(temp_vault) in path.parents
