"""Orchestration: gather -> LLM leaf -> compose markdown -> write note + DB row.

Control flow here is ordinary Python. The LLM is a single bounded leaf call
(llm.summarize). This module ties the pieces together and records a
worker_runs audit row around the whole thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbmod
from .config import WORKER_NAME, Config
from .gather import NoteInput, gather_recent_notes
from .llm import LLMError, summarize
from .vault import target_path, write_note


@dataclass(frozen=True)
class BriefResult:
    """Outcome of a brief build, for the CLI to report."""

    title: str
    body_md: str
    markdown: str
    note_path: Path | None
    brief_id: int | None
    dry_run: bool


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def compose_markdown(
    *,
    title: str,
    created_at: str,
    window: str,
    body_md: str,
    notes: tuple[NoteInput, ...],
) -> str:
    """Compose the full note markdown: frontmatter + body + sources.

    Deterministic string assembly — no LLM.
    """
    lines = [
        "---",
        f"title: {title}",
        f"created: {created_at}",
        f"window: {window}",
        "generator: sonar/brief-builder",
        "---",
        "",
        f"# {title}",
        "",
        body_md.strip(),
        "",
    ]
    if notes:
        lines.append("## Sources")
        lines.append("")
        for note in notes:
            lines.append(f"- {note.title}")
        lines.append("")
    return "\n".join(lines)


def _produce_markdown(
    config: Config, *, title: str, created_at: str
) -> tuple[tuple[NoteInput, ...], str, str]:
    """Gather notes, run the single LLM leaf call, compose the note markdown.

    The one bounded LLM call (summarize) lives here — it is the most likely
    failure point (Ollama down/timeout), so on real runs it is invoked inside
    the audited block in build_brief().
    """
    notes = gather_recent_notes(
        config.vault,
        max_notes=config.max_notes,
        max_chars_per_note=config.max_chars_per_note,
    )
    body_md = summarize(config, notes)
    markdown = compose_markdown(
        title=title,
        created_at=created_at,
        window=config.window,
        body_md=body_md,
        notes=notes,
    )
    return notes, body_md, markdown


def build_brief(config: Config, *, now: datetime | None = None) -> BriefResult:
    """Run the full brief build for one invocation.

    On dry-run: gather + LLM + compose, write NOTHING (no note, no DB row).
    The CLI prints the composed markdown.

    On a real run: open the state DB and record a worker_runs audit row FIRST,
    then gather + LLM + write the note + insert the brief row. Because the audit
    row is opened before the LLM call, a failed run (the common case: Ollama
    down/timeout) is still recorded with status='error' — which is the whole
    point of the worker_runs table.

    Raises:
        LLMError: if the leaf call fails (recorded in worker_runs, then re-raised).
    """
    now = now or datetime.now(timezone.utc)
    created_at = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    day = _today(now.astimezone())
    window_label = config.window.capitalize()
    title = f"{window_label} Brief — {day}"

    if config.dry_run:
        _notes, body_md, markdown = _produce_markdown(
            config, title=title, created_at=created_at
        )
        return BriefResult(
            title=title,
            body_md=body_md,
            markdown=markdown,
            note_path=None,
            brief_id=None,
            dry_run=True,
        )

    # Real run: record the audit row FIRST so any failure below is captured.
    conn = dbmod.init_db(config.db_path)
    try:
        run_id = dbmod.start_worker_run(conn, WORKER_NAME, created_at)
        try:
            _notes, body_md, markdown = _produce_markdown(
                config, title=title, created_at=created_at
            )
            note_path = write_note(
                config.vault,
                target_path(
                    config.vault, window=config.window, day=day, now=now.astimezone()
                ),
                markdown,
            )
            brief_id = dbmod.insert_brief(
                conn,
                created_at=created_at,
                window=config.window,
                title=title,
                body_md=body_md,
                note_path=str(note_path),
            )
            dbmod.finish_worker_run(
                conn, run_id, _utc_now_iso(), "ok", detail=f"brief_id={brief_id}"
            )
        except Exception as exc:  # noqa: BLE001 - record then re-raise
            dbmod.finish_worker_run(
                conn, run_id, _utc_now_iso(), "error", detail=str(exc)[:500]
            )
            raise
    finally:
        conn.close()

    return BriefResult(
        title=title,
        body_md=body_md,
        markdown=markdown,
        note_path=note_path,
        brief_id=brief_id,
        dry_run=False,
    )
