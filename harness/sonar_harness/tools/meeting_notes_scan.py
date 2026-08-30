"""The last time the user met these people — a read-only scan of Sonar Notes.

``voice/notes/store.py`` saves each diarized session to ``<vault>/Sonar/Notes/``
with the people in its ``speakers:`` frontmatter and the decisions under an
``## AI Overview`` heading. That makes the folder the one place that already
answers "what did we decide with them last time", so ``meeting.prep`` reads it
directly rather than hoping the RAG index has been rebuilt since the last
meeting.

Read-only and side-effect free: it never creates the folder, never writes, and
an unreadable note is skipped rather than fatal.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

log = logging.getLogger("sonar.tools.meeting_notes_scan")

_NOTES_DIR = Path("Sonar") / "Notes"  # where voice/notes/store.py saves sessions
_NOTES_LIMIT = 3
_OVERVIEW_CHARS = 600

# voice/notes/store.py's note shape: JSON-ish frontmatter, then "## <heading>"s.
_FM_CREATED = re.compile(r"^created:\s*(?P<value>.+)$", re.MULTILINE)
_FM_SPEAKERS = re.compile(r"^speakers:\s*(?P<value>.+)$", re.MULTILINE)
_NOTE_TITLE = re.compile(r"^#\s+(?P<value>.+?)\s*$", re.MULTILINE)
_NOTE_HEADING = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)
_OVERVIEW_HEADING = "AI Overview"


@dataclass(frozen=True)
class NoteHit:
    """A past Sonar Notes session that involved one of the people we're prepping for."""

    path: str            # vault-relative, e.g. "Sonar/Notes/Design review.md"
    title: str
    created: str         # "YYYY-MM-DD HH:MM" as store.py writes it
    speakers: tuple[str, ...]
    overview: str        # the "## AI Overview" body — what was decided


def _section_body(text: str, heading: str) -> str:
    """The body of a ``## <heading>`` block, up to the next ``## `` heading.

    Stopping at the next heading is load-bearing, not tidiness: the section after
    ``## AI Overview`` is ``## Transcript``, so running past it would paste a
    whole meeting's raw speech into the prep for the model to read out.
    """
    marks = [
        (m.group("name").strip().lower(), m.end(), m.start())
        for m in _NOTE_HEADING.finditer(text)
    ]
    for i, (name, body_start, _) in enumerate(marks):
        if name != heading.lower():
            continue
        stop = marks[i + 1][2] if i + 1 < len(marks) else len(text)
        return text[body_start:stop].strip()
    return ""


def _parse_speakers(raw: str) -> tuple[str, ...]:
    """Read store.py's ``speakers: ["A", "B"]`` frontmatter (a JSON flow seq)."""
    raw = raw.strip()
    if not raw or raw == "[]":
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Hand-edited frontmatter: fall back to a bare comma split rather than
        # losing the whole note's people.
        data = raw.strip("[]").split(",")
    if not isinstance(data, list):
        return ()
    return tuple(s for s in (str(item).strip().strip('"').strip() for item in data) if s)


def _match_value(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group("value").strip() if match else ""


def _read_meeting_note(text: str, path: Path, vault: Path) -> NoteHit:
    """One saved notes session, in the shape voice/notes/store.py writes."""
    return NoteHit(
        path=path.relative_to(vault).as_posix(),
        title=_match_value(_NOTE_TITLE, text) or path.stem,
        created=_match_value(_FM_CREATED, text),
        speakers=_parse_speakers(_match_value(_FM_SPEAKERS, text)),
        overview=_section_body(text, _OVERVIEW_HEADING),
    )


def _mtime_stamp(path: Path) -> str:
    """A sort key for a note whose frontmatter lost its ``created:`` line."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


def scan_meeting_notes(
    vault_path: Path | str, needles: Sequence[str], limit: int = _NOTES_LIMIT
) -> tuple[NoteHit, ...]:
    """The most recent ``Sonar/Notes/`` sessions involving any of ``needles``.

    Matches on the note's title and its ``speakers:`` frontmatter — the two
    places a person is named authoritatively — rather than the transcript, where
    a passing mention would drown the real answer. ``created`` is written as
    ``YYYY-MM-DD HH:MM``, which sorts lexicographically, so newest-first needs no
    date parsing.
    """
    wanted = [n.strip().lower() for n in needles if isinstance(n, str) and n.strip()]
    if not wanted:
        return ()  # nobody to look for; scanning the folder could only mislead
    vault = Path(vault_path)
    folder = vault / _NOTES_DIR
    if not folder.is_dir():
        return ()

    ranked: list[tuple[str, NoteHit]] = []
    try:
        paths = sorted(folder.glob("*.md"))
    except OSError as exc:
        log.warning("could not list %s (%s)", folder, exc)
        return ()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue  # one unreadable note shouldn't cost the whole scan
        hit = _read_meeting_note(text, path, vault)
        haystack = f"{hit.title} {' '.join(hit.speakers)}".lower()
        if any(needle in haystack for needle in wanted):
            ranked.append((hit.created or _mtime_stamp(path), hit))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return tuple(hit for _, hit in ranked[: max(1, limit)])


def render_note_hits(hits: Sequence[NoteHit]) -> str:
    """Past sessions as "when — what it was — what was decided"."""
    if not hits:
        return "No past Sonar meeting notes match."
    lines: list[str] = []
    for hit in hits:
        who = ", ".join(hit.speakers) or "unknown"
        overview = " ".join(hit.overview.split())
        if len(overview) > _OVERVIEW_CHARS:
            overview = overview[: _OVERVIEW_CHARS - 1] + "…"
        lines.append(
            f"- {hit.created or '(undated)'} — {hit.title} (with {who}): "
            f"{overview or '(no overview recorded)'}"
        )
    return "\n".join(lines)
