"""Render + save a finished notes session into the vault (Sonar/Notes/).

Same safety posture as the harness's note.capture: the filename is slugified
down to one safe stem (no separators, no dots -> no traversal), writes are
confined to the Sonar-authored ``Sonar/Notes/`` folder, and the write itself is
atomic (temp file + os.replace). Unlike note.capture this CREATES one note per
session rather than appending; a name collision gets a ``-2``/``-3`` suffix,
and re-saving the SAME session overwrites its own file (the controller pins the
path after the first save).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from notes.session import SessionState, display_name

_NOTES_DIR = Path("Sonar") / "Notes"
_MAX_SLUG_LEN = 80
_UNSAFE = re.compile(r"[^A-Za-z0-9 _-]+")
_WS = re.compile(r"\s+")


def slug_for(title: str | None, fallback: str) -> str:
    """Reduce a session title to one safe filename stem (see module docstring)."""
    cleaned = _WS.sub(" ", _UNSAFE.sub(" ", title or "")).strip()[:_MAX_SLUG_LEN].strip()
    return cleaned or fallback


def _mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def _yaml_flow_seq(names: list[str]) -> str:
    """Render display names as a valid YAML flow sequence. A user-chosen name
    is untrusted text: rendered bare, one containing ':', a newline, or a YAML
    indicator could break out of the frontmatter and inject a top-level key.
    Each name is emitted as a JSON string instead — JSON is a subset of YAML
    1.2, so the escaping is both valid and injection-proof."""
    return "[" + ", ".join(json.dumps(n, ensure_ascii=False) for n in names) + "]"


def render_note(state: SessionState, now: datetime) -> str:
    """The full markdown note: frontmatter, AI overview, diarized transcript."""
    speakers = [display_name(state, sid) for sid, _ in state.names]
    lines = [
        "---",
        f"created: {now.strftime('%Y-%m-%d %H:%M')}",
        "type: meeting-notes",
        f"speakers: {_yaml_flow_seq(speakers)}" if speakers else "speakers: []",
        "source: sonar-notes",
        "---",
        "",
        f"# {state.title}",
        "",
        "## AI Overview",
        "",
        state.summary_md.strip() or "_(no AI overview)_",
        "",
        "## Transcript",
        "",
    ]
    for seg in state.segments:
        lines.append(f"**{display_name(state, seg.speaker)}** ({_mmss(seg.t0)}): {seg.text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def pick_path(vault: Path, state: SessionState, now: datetime) -> Path:
    """First free ``Sonar/Notes/<slug>.md`` path (collisions get -2, -3, …)."""
    slug = slug_for(state.title, fallback=f"Notes {now.strftime('%Y-%m-%d %H-%M')}")
    base = vault / _NOTES_DIR
    path = base / f"{slug}.md"
    n = 2
    while path.exists():
        path = base / f"{slug}-{n}.md"
        n += 1
    return path


def save_note(
    state: SessionState, vault: Path, now: datetime, path: Path | None = None
) -> Path:
    """Write the note atomically; returns the absolute path written.

    ``path`` re-saves an already-saved session in place; otherwise a fresh
    collision-free path is chosen. Raises OSError on filesystem trouble — the
    caller surfaces that to the UI rather than losing the transcript silently.
    """
    vault = Path(vault)
    if not vault.is_dir():
        raise OSError(f"vault path {str(vault)!r} is not a directory")
    target = path if path is not None else pick_path(vault, state, now)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(render_note(state, now), encoding="utf-8")
    os.replace(tmp, target)  # atomic swap on the same filesystem
    return target
