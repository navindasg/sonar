"""Gather bounded inputs from the Obsidian vault.

Plain, deterministic code: find the N most-recently-modified markdown notes,
EXCLUDING the Sonar/ output folder, and extract a title + a short first-line
excerpt from each. No LLM here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import EXCLUDED_TOP_DIR


@dataclass(frozen=True)
class NoteInput:
    """One gathered note: its title and a short excerpt."""

    path: Path
    title: str
    excerpt: str
    mtime: float


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    """True if the note lives under the Sonar/ output tree (case-insensitive)."""
    return bool(rel_parts) and rel_parts[0].lower() == EXCLUDED_TOP_DIR.lower()


def _extract_title(path: Path, text: str) -> str:
    """Title = first markdown H1 if present, else the filename stem."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or path.stem
    return path.stem


def _extract_excerpt(text: str, max_chars: int) -> str:
    """First non-empty, non-heading line, truncated to max_chars."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(stripped) > max_chars:
            return stripped[: max_chars - 1].rstrip() + "…"
        return stripped
    return ""


def gather_recent_notes(
    vault: Path, *, max_notes: int, max_chars_per_note: int
) -> tuple[NoteInput, ...]:
    """Return up to `max_notes` most-recently-modified notes as an immutable tuple.

    Silently skips unreadable files (permissions, races) — a single bad note
    must never abort the whole gather.

    Args:
        vault: absolute path to the Obsidian vault root.
        max_notes: hard cap on how many notes to return.
        max_chars_per_note: excerpt truncation length.
    """
    if not vault.is_dir():
        return ()

    candidates: list[tuple[float, Path]] = []
    for md in vault.rglob("*.md"):
        if not md.is_file():
            continue
        try:
            rel_parts = md.relative_to(vault).parts
        except ValueError:  # pragma: no cover - rglob stays under vault
            continue
        if _is_excluded(rel_parts):
            continue
        try:
            mtime = md.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, md))

    # Most recent first; deterministic tiebreak by path for stable output.
    candidates.sort(key=lambda pair: (-pair[0], str(pair[1])))

    notes: list[NoteInput] = []
    for mtime, md in candidates[:max_notes]:
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        notes.append(
            NoteInput(
                path=md,
                title=_extract_title(md, text),
                excerpt=_extract_excerpt(text, max_chars_per_note),
                mtime=mtime,
            )
        )
    return tuple(notes)
