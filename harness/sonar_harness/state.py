"""SQLite live-state handle for the harness.

Thin wrapper over a WAL-mode SQLite connection initialized from the
checked-in ``state/schema.sql`` (briefs + worker_runs). This is the fast,
disposable layer workers write and the harness reads; durable/semantic
memory lives in the Obsidian vault via RAG, not here.

The DB file lives OUTSIDE the vault and is gitignored. The harness only
*reads* it in this pass (via the ``state_read`` tool); the write path is
owned by the worker stream.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("sonar.state")

# Repo-root-relative default: harness/sonar_harness/state.py -> repo/state/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_PATH = _REPO_ROOT / "state" / "schema.sql"
DEFAULT_DB_PATH = _REPO_ROOT / "state" / "sonar.sqlite"


class State:
    """Owns one SQLite connection, opened in WAL mode and schema-initialized."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(
        cls,
        db_path: Path | str | None = None,
        schema_path: Path | str | None = None,
    ) -> "State":
        """Open (creating if needed) the DB and apply the idempotent schema."""
        path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        schema = Path(schema_path) if schema_path is not None else DEFAULT_SCHEMA_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # journal_mode is per-database runtime state, set in code (not the .sql).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if schema.exists():
            conn.executescript(schema.read_text(encoding="utf-8"))
            conn.commit()
            log.info("state schema applied from %s", schema)
        else:
            log.warning("schema file missing at %s; DB left uninitialized", schema)
        return cls(conn)

    def close(self) -> None:
        self.conn.close()
