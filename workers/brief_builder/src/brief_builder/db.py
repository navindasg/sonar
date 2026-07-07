"""SQLite live-state access: open/init (WAL), parameterized inserts.

The schema is the single source of truth in `state/schema.sql` at the repo
root. WAL mode is enabled here in code (per DECISIONS.md) because
`journal_mode` is a runtime pragma, not a DDL statement.

All writes use parameterized queries — never string interpolation.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


class SchemaNotFoundError(FileNotFoundError):
    """Raised when state/schema.sql cannot be located."""


def _find_schema() -> Path:
    """Locate `state/schema.sql`.

    Resolution order:
      1. SONAR_SCHEMA env var (explicit path).
      2. Walk up from this file looking for a `state/schema.sql`.

    Raises:
        SchemaNotFoundError: if the schema file cannot be found.
    """
    override = os.environ.get("SONAR_SCHEMA")
    if override:
        candidate = Path(override).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise SchemaNotFoundError(f"SONAR_SCHEMA points to missing file: {candidate}")

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "state" / "schema.sql"
        if candidate.is_file():
            return candidate
    raise SchemaNotFoundError(
        "could not locate state/schema.sql; set SONAR_SCHEMA to its absolute path"
    )


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + sane pragmas.

    Creates the parent directory if needed. The caller owns closing the
    connection (use as a context manager where possible).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # WAL enables concurrent readers (harness) alongside the writer (worker).
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) and apply the schema. Idempotent."""
    schema_sql = _find_schema().read_text(encoding="utf-8")
    conn = connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def start_worker_run(
    conn: sqlite3.Connection, worker: str, started_at: str
) -> int:
    """Record the start of a worker run; return its row id."""
    cur = conn.execute(
        "INSERT INTO worker_runs (worker, started_at, status) VALUES (?, ?, ?)",
        (worker, started_at, "running"),
    )
    conn.commit()
    run_id = cur.lastrowid
    if run_id is None:  # pragma: no cover - sqlite always sets lastrowid here
        raise sqlite3.Error("failed to obtain worker_run id")
    return run_id


def finish_worker_run(
    conn: sqlite3.Connection,
    run_id: int,
    finished_at: str,
    status: str,
    detail: str | None = None,
) -> None:
    """Mark a worker run finished with a terminal status ('ok' | 'error')."""
    conn.execute(
        "UPDATE worker_runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
        (finished_at, status, detail, run_id),
    )
    conn.commit()


def insert_brief(
    conn: sqlite3.Connection,
    *,
    created_at: str,
    window: str,
    title: str,
    body_md: str,
    note_path: str | None,
) -> int:
    """Insert a brief row; return its id. Uses parameterized SQL only."""
    cur = conn.execute(
        """
        INSERT INTO briefs (created_at, window, title, body_md, note_path)
        VALUES (?, ?, ?, ?, ?)
        """,
        (created_at, window, title, body_md, note_path),
    )
    conn.commit()
    brief_id = cur.lastrowid
    if brief_id is None:  # pragma: no cover
        raise sqlite3.Error("failed to obtain brief id")
    return brief_id
