"""SQLite live-state handle for the harness.

Thin wrapper over a WAL-mode SQLite connection initialized from the
checked-in ``state/schema.sql`` (briefs + worker_runs + todos). This is the
fast, disposable layer: workers write briefs/worker_runs, the harness writes
its own ``todos`` (todo_add) and reads all three. Durable/semantic memory —
including the user's OWN to-dos — lives in the Obsidian vault via RAG, not here.

The DB file lives OUTSIDE the vault and is gitignored.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("sonar.state")

# Repo-root-relative default: harness/sonar_harness/state.py -> repo/state/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_PATH = _REPO_ROOT / "state" / "schema.sql"
DEFAULT_DB_PATH = _REPO_ROOT / "state" / "sonar.sqlite"

# The assistant's captured todos are disposable working memory, not a durable
# tracker — so they auto-expire. An undated todo lives ~this long from capture;
# a dated todo lives through its due day (so a future 'due' isn't nuked early).
TODO_TTL_HOURS = 24


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

    def expire_todos(
        self, *, now: datetime | None = None, ttl_hours: int = TODO_TTL_HOURS
    ) -> int:
        """Delete stale ``todos`` rows; return how many were removed.

        Lazy expiry (called before todo reads/writes — no background job):
          * open + undated  -> gone ``ttl_hours`` after ``created_at``
          * open + dated     -> gone once its ``due`` day has passed (lives
                                through the due day itself)
          * done             -> cleaned up ``ttl_hours`` after ``created_at``

        ``created_at`` is stored as an ISO-8601 UTC string, so it compares
        lexically against a UTC cutoff; ``due`` is a local calendar date, so it
        compares against the local "today".
        """
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=ttl_hours)).isoformat()
        today = now.astimezone().date().isoformat()
        cur = self.conn.execute(
            "DELETE FROM todos WHERE "
            "  (status = 'open' AND due IS NULL AND created_at < ?) OR "
            "  (status = 'open' AND due IS NOT NULL AND due < ?) OR "
            "  (status = 'done' AND created_at < ?)",
            (cutoff, today, cutoff),
        )
        self.conn.commit()
        if cur.rowcount:
            log.info("expired %d stale todo(s)", cur.rowcount)
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()
