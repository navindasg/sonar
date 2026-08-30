"""Durable step-event history (SQLite) — the layer under ``events.EventSink``.

The sink keeps a bounded in-memory ring so the live turn renders instantly;
this is the half that survives a harness restart, so "what did Sonar do, and
why" is still answerable tomorrow. One row per emitted event in the SAME
``state/schema.sql`` DB the rest of the harness uses (``events`` table).

Two rules shape every method here:

  * **A failed write must never fail a turn.** The tool loop emits events
    inline, on the turn's own thread. So each public method owns its failure:
    ``append`` returns False, ``query`` returns None, ``prune`` returns 0 — and
    a log line explains it. Nothing propagates out of this module.
  * **Retention is housekeeping, not correctness.** Pruning is lazy — once on
    open, then every ``_PRUNE_EVERY`` appends, always on the caller's thread.
    No background timer: Sonar does nothing on a schedule it wasn't asked for.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from sonar_harness.state import State

log = logging.getLogger("sonar.event_store")

MS_PER_DAY = 86_400_000

RETAIN_DAYS_ENV = "SONAR_EVENTS_RETAIN_DAYS"
DEFAULT_RETAIN_DAYS = 30

DEFAULT_LIMIT = 100

# Appends between retention sweeps. A sweep is one indexed range-delete, but at
# ~5 events a turn this still means "a few times a week" rather than per-turn.
_PRUNE_EVERY = 500

# How long a write waits for SQLite's single write lock before giving up.
_BUSY_TIMEOUT_MS = 250

_COLUMNS = "turn_id, ts, step, tool, detail, status"


def _tune_for_inline_writes(conn: sqlite3.Connection) -> None:
    """Trade a sliver of durability for never stalling the turn thread.

    Both pragmas are per-CONNECTION, so this tuning covers step-event writes
    only — the harness's ``State`` handle (todos, briefs) keeps SQLite's
    conservative defaults.

      * ``busy_timeout``: SQLite allows exactly one writer, and sqlite3 waits
        **5 seconds** for it by default. These appends run inline on the turn's
        own thread, so an unrelated writer would hold a live answer open for
        seconds; a quarter second and then ring-only is the better trade.
      * ``synchronous=NORMAL``: under WAL this still survives a process crash
        (the log replays) — only an OS crash or power cut can cost the last few
        events. Worth it to keep an fsync-per-step out of the loop the user is
        waiting on.
    """
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS:d}")
    conn.execute("PRAGMA synchronous=NORMAL")


def retain_days() -> int:
    """How many days of history to keep, from the environment (default 30).

    Read per call rather than cached at import: the harness is a long-lived
    daemon, and a value that only takes effect after a reinstall is a trap.
    Anything unusable (non-integer, zero, negative) costs the default, never a
    crash — retention is housekeeping and must not be able to break startup.
    """
    raw = os.environ.get(RETAIN_DAYS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_RETAIN_DAYS
    try:
        days = int(raw)
    except ValueError:
        log.warning(
            "%s=%r is not an integer; keeping %d days", RETAIN_DAYS_ENV, raw, DEFAULT_RETAIN_DAYS
        )
        return DEFAULT_RETAIN_DAYS
    if days <= 0:
        log.warning(
            "%s=%d is not a positive number of days; keeping %d",
            RETAIN_DAYS_ENV,
            days,
            DEFAULT_RETAIN_DAYS,
        )
        return DEFAULT_RETAIN_DAYS
    return days


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    """Rebuild the exact wire dict the in-memory ring yields.

    ``tool``/``detail`` are OMITTED when NULL rather than serialized as null:
    the overlay bridge and voice loop were written against a ring that simply
    never sets an absent key, so emitting explicit nulls would be a wire change.
    """
    event: dict[str, Any] = {
        "step": row["step"],
        "status": row["status"],
        "turn_id": row["turn_id"],
        "ts": row["ts"],
    }
    if row["tool"] is not None:
        event["tool"] = row["tool"]
    if row["detail"] is not None:
        event["detail"] = row["detail"]
    return event


class EventStore:
    """Append-only step-event history on its own SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # sqlite3 serializes statements, but this connection is shared between
        # the turn thread (writes) and the event loop (/events reads), so the
        # lock keeps a read from interleaving with a write's commit.
        self._lock = Lock()
        self._appends_since_prune = 0

    @classmethod
    def open(
        cls,
        db_path: Path | str | None = None,
        schema_path: Path | str | None = None,
    ) -> "EventStore":
        """Open the shared state DB on this store's OWN connection.

        Connection handling (WAL, row factory, idempotent schema) is ``State``'s
        — the events table ships in the same ``state/schema.sql``, so we reuse
        that opener instead of drifting a second copy of it. The connection is
        deliberately separate from the harness's ``State`` handle: event writes
        land inline on the turn thread while ``/events`` reads run on the event
        loop, under WAL two connections keep them out of each other's way, and
        the latency pragmas below then apply to history alone.
        """
        conn = State.open(db_path=db_path, schema_path=schema_path).conn
        _tune_for_inline_writes(conn)
        store = cls(conn)
        store.prune()  # one sweep per process start, before anything reads
        return store

    @classmethod
    def try_open(
        cls,
        db_path: Path | str | None = None,
        schema_path: Path | str | None = None,
    ) -> "EventStore | None":
        """``open`` that returns None instead of raising.

        A DB we cannot open costs the user their history; it must never cost
        them the harness, so startup wires this and carries on ring-only.
        """
        try:
            return cls.open(db_path=db_path, schema_path=schema_path)
        except (sqlite3.Error, OSError) as exc:
            log.warning("step-event history disabled: could not open store (%s)", exc)
            return None

    def append(self, event: Mapping[str, Any]) -> bool:
        """Persist one already-normalized event; True if it landed.

        Called from inside the turn loop, so it swallows everything: a full
        disk or a locked DB is worth a log line, never a lost answer.
        """
        try:
            with self._lock:
                self.conn.execute(
                    f"INSERT INTO events ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event.get("turn_id"),
                        int(event.get("ts") or 0),
                        event.get("step"),
                        event.get("tool"),
                        event.get("detail"),
                        event.get("status"),
                    ),
                )
                self.conn.commit()
                self._appends_since_prune += 1
                sweep_due = self._appends_since_prune >= _PRUNE_EVERY
        except Exception as exc:  # noqa: BLE001 - a broken write must not fail a turn
            log.warning("could not persist step-event (%s)", exc)
            return False
        if sweep_due:
            self.prune()
        return True

    def query(
        self,
        *,
        since: int | None = None,
        turn_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict[str, Any]] | None:
        """The newest ``limit`` matching events, oldest-first.

        ``since`` is an INCLUSIVE epoch-ms floor (a poller wanting strictly-new
        events passes ``last_ts + 1``). Returns None — not an empty list — when
        the read itself failed, so the caller can tell "no history" apart from
        "no answer" and fall back to the ring.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(int(since))
        if turn_id is not None:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # Newest-first with the limit applied in SQL (so the index does the
        # work), then reversed: callers get the ring's oldest-first order.
        sql = f"SELECT {_COLUMNS} FROM events{where} ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(int(limit))
        try:
            with self._lock:
                rows = self.conn.execute(sql, params).fetchall()
        except Exception as exc:  # noqa: BLE001 - a failed read degrades to the ring
            log.warning("could not read step-event history (%s)", exc)
            return None
        return [_row_to_event(row) for row in reversed(rows)]

    def prune(self, *, now_ms: int | None = None, days: int | None = None) -> int:
        """Delete events past the retention window; return how many went.

        Lazy expiry in the same spirit as ``State.expire_todos``: no background
        job, just one indexed range-delete on ``ts`` whenever a caller happens
        to be here. ``now_ms``/``days`` are injectable so tests can pin both.
        """
        window = days if days is not None else retain_days()
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        cutoff = now - window * MS_PER_DAY
        try:
            with self._lock:
                # Count down from here whatever happens next: a sweep that
                # loses the write lock must wait for the NEXT window, not leave
                # every following append due and paying that wait inline.
                self._appends_since_prune = 0
                cur = self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                self.conn.commit()
                removed = cur.rowcount
        except Exception as exc:  # noqa: BLE001 - housekeeping never breaks a turn
            log.warning("could not prune step-event history (%s)", exc)
            return 0
        if removed > 0:
            log.info("pruned %d step-event(s) older than %d day(s)", removed, window)
        return max(removed, 0)

    def close(self) -> None:
        """Close the connection. Safe to call on an already-closed store."""
        try:
            self.conn.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log.warning("could not close step-event store (%s)", exc)
