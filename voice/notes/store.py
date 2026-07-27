"""Render + save a finished notes session into the vault (Sonar/Notes/).

Same safety posture as the harness's note.capture: the filename is slugified
down to one safe stem (no separators, no dots -> no traversal), writes are
confined to the Sonar-authored ``Sonar/Notes/`` folder, and the write itself is
atomic (temp file + os.replace). Unlike note.capture this CREATES one note per
session rather than appending; a name collision gets a ``-2``/``-3`` suffix,
and re-saving the SAME session overwrites its own file (the controller pins the
path after the first save).

The note also carries a ``## My Action Items`` section: the USER's own
commitments from the AI overview, restated as dated ``- [ ]`` checkboxes so the
harness's vault-wide checkbox scan (``todo_list``) can pick them up and they
stop being inert prose. The whole note is re-rendered from ``SessionState`` on
every save, so that section is regenerated rather than appended — re-saving a
session can never duplicate a checkbox.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Sequence

from notes.session import SessionState, display_name
from notes.summarize import USER_NAME_ENV, configured_user_names, user_action_items

log = logging.getLogger("sonar.notes.store")

_NOTES_DIR = Path("Sonar") / "Notes"
_MAX_SLUG_LEN = 80
_UNSAFE = re.compile(r"[^A-Za-z0-9 _-]+")
_WS = re.compile(r"\s+")

_MY_ACTIONS_HEADING = "## My Action Items"
# An Obsidian Tasks-plugin due date already on the task text — don't add a second.
_HAS_DUE_DATE = re.compile(r"📅\s*\d{4}-\d{2}-\d{2}")


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


def my_action_lines(tasks: Sequence[str], day: str) -> list[str]:
    """The user's action items as Obsidian task checkboxes, due ``day``.

    The date is not decoration: ``todo_list``'s ``today``/``overdue`` filters —
    the ones ``daily.brief`` uses — only compare tasks that HAVE a date, and a
    meeting note's filename isn't a daily-note date, so an unstamped item could
    never reach the brief. The session's own date is the honest due date: the
    user took the commitment that day, and it rolls into "overdue" after.
    """
    return [
        f"- [ ] {task}" if _HAS_DUE_DATE.search(task) else f"- [ ] {task} 📅 {day}"
        for task in tasks
    ]


def _my_actions_block(
    state: SessionState, now: datetime, user_names: Sequence[str]
) -> list[str]:
    """The ``## My Action Items`` section, or nothing at all when the user owns
    no items (or isn't identified — see summarize.USER_NAME_ENV). An empty
    section would be noise in every meeting the user only listened to."""
    tasks = user_action_items(state.summary_md, user_names)
    if not tasks:
        return []
    return [_MY_ACTIONS_HEADING, "", *my_action_lines(tasks, now.strftime("%Y-%m-%d")), ""]


def render_note(
    state: SessionState, now: datetime, user_names: Sequence[str] | None = None
) -> str:
    """The full markdown note: frontmatter, AI overview, the user's action items,
    diarized transcript. ``user_names`` defaults to the configured identity."""
    names = configured_user_names() if user_names is None else user_names
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
    ]
    lines += _my_actions_block(state, now, names)
    lines += ["## Transcript", ""]
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
    state: SessionState,
    vault: Path,
    now: datetime,
    path: Path | None = None,
    *,
    user_names: Sequence[str] | None = None,
) -> Path:
    """Write the note atomically; returns the absolute path written.

    ``path`` re-saves an already-saved session in place; otherwise a fresh
    collision-free path is chosen. Raises OSError on filesystem trouble — the
    caller surfaces that to the UI rather than losing the transcript silently.
    """
    vault = Path(vault)
    if not vault.is_dir():
        raise OSError(f"vault path {str(vault)!r} is not a directory")
    names = configured_user_names() if user_names is None else user_names
    _warn_if_unidentified(state, names)
    target = path if path is not None else pick_path(vault, state, now)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(render_note(state, now, names), encoding="utf-8")
    os.replace(tmp, target)  # atomic swap on the same filesystem
    return target


def _warn_if_unidentified(state: SessionState, user_names: Sequence[str]) -> None:
    """Say in the log why a meeting full of action items produced no checkboxes
    — otherwise the missing section looks like a bug rather than a setting."""
    if user_names or "Action Items" not in state.summary_md:
        return
    log.info(
        "notes: action items found but %s is unset — set it to the name(s) you "
        "label yourself with in the notes UI to get your own to-do checkboxes",
        USER_NAME_ENV,
    )
