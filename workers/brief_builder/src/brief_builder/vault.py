"""Vault-write safety.

CRITICAL invariant: this worker only EVER writes under
    <vault>/Sonar/Briefs/<YYYY-MM-DD>-<window>.md
It must never overwrite or touch any existing user note outside that folder.
If the target file already exists, a timestamped variant is written instead of
clobbering it.

Every path is resolved and re-checked to be inside the output dir before any
write happens — belt and suspenders against traversal via odd window/date
values.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .config import OUTPUT_SUBDIR


class UnsafeWritePathError(RuntimeError):
    """Raised when a computed write path would escape the Briefs output dir."""


_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize(segment: str) -> str:
    """Reduce an arbitrary string to a safe single filename segment."""
    cleaned = _SAFE_SEGMENT.sub("-", segment).strip("-")
    return cleaned or "brief"


def output_dir(vault: Path) -> Path:
    """The one directory this worker may write to: <vault>/Sonar/Briefs."""
    return (vault / OUTPUT_SUBDIR).resolve()


def _assert_inside(base: Path, candidate: Path) -> None:
    """Raise unless `candidate` is `base` or lives beneath it."""
    base_r = base.resolve()
    cand_r = candidate.resolve()
    if cand_r != base_r and base_r not in cand_r.parents:
        raise UnsafeWritePathError(
            f"refusing to write outside {base_r}: {cand_r}"
        )


def target_path(
    vault: Path, *, window: str, day: str, now: datetime | None = None
) -> Path:
    """Compute the collision-safe target note path inside the Briefs dir.

    If `<day>-<window>.md` already exists, return a timestamped variant
    `<day>-<window>-<HHMMSS>.md` (and keep suffixing if that somehow exists)
    so an existing brief is never clobbered.
    """
    out = output_dir(vault)
    base_name = f"{_sanitize(day)}-{_sanitize(window)}"
    candidate = out / f"{base_name}.md"
    _assert_inside(out, candidate)

    if not candidate.exists():
        return candidate

    stamp = (now or datetime.now()).strftime("%H%M%S")
    variant = out / f"{base_name}-{stamp}.md"
    counter = 1
    while variant.exists():
        variant = out / f"{base_name}-{stamp}-{counter}.md"
        counter += 1
    _assert_inside(out, variant)
    return variant


def write_note(vault: Path, path: Path, content: str) -> Path:
    """Write `content` to `path`, enforcing the output-dir invariant.

    Creates the Briefs directory as needed. Refuses to overwrite an existing
    file (callers get collision-free paths from `target_path`).

    Returns the path written.
    """
    out = output_dir(vault)
    _assert_inside(out, path)
    out.mkdir(parents=True, exist_ok=True)
    # 'x' mode fails loudly if the file exists — never clobber.
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(content)
    return path
