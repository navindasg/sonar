"""Step-event sink for the overlay "steps taken" panel (CONTRACTS.md §3).

The harness emits one compact JSON event per loop step. The canonical shape:

    {"step": "tool", "tool": "search", "detail": "<query>", "status": "ok"}

``step`` is required and one of:
    turn_start | tool | tool_result_summary | model_switch | final
``tool`` is present on ``tool`` / ``tool_result_summary``. ``detail`` is a
short (<=120 char) human string. ``status`` is ok | error | pending (default
ok). The emitter attaches ``turn_id`` and ``ts`` (epoch ms) for correlation;
consumers ignore unknown fields/kinds.

This module keeps a bounded in-memory ring the ``/events`` endpoint serves so
the overlay can poll it, and — when a store is wired in — mirrors every event
into SQLite (``event_store``) so history outlives the process. The ring stays
the fast path and the safety net; the store is what a Console/audit surface
reads back after a restart. A later pass can swap the transport for the
localhost WebSocket without touching the emit call sites.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from threading import Lock
from typing import Any, Deque

from sonar_harness.event_store import DEFAULT_LIMIT, EventStore

log = logging.getLogger("sonar.events")

_MAX_DETAIL = 120
_MAX_LIMIT = 2000
_VALID_STEPS = frozenset(
    {"turn_start", "tool", "tool_result_summary", "model_switch", "final"}
)


def _clamp_limit(limit: int) -> int:
    """Keep a caller-supplied limit sane — and stop a bad one meaning "everything".

    ``/events`` is public to anything on localhost, and a negative or absurd
    limit used to fall through to a raw list slice with surprising results.
    """
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, _MAX_LIMIT))


class EventSink:
    """Thread-safe bounded ring of step-events, optionally mirrored to SQLite."""

    def __init__(self, maxlen: int = 512, *, store: EventStore | None = None) -> None:
        self._events: Deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._store = store
        self._lock = Lock()

    def emitter(self, turn_id: str):
        """Return a ``emit(event: dict)`` bound to one turn_id (the ToolContext sink)."""

        def emit(event: dict[str, Any]) -> None:
            self.record(turn_id, event)

        return emit

    def record(self, turn_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Normalize, stamp, store, and log one event; return the stored dict."""
        step = event.get("step")
        if step not in _VALID_STEPS:
            log.warning("dropping event with unknown step=%r", step)
            # Still store it — consumers ignore unknown kinds — but flag it.
        detail = event.get("detail")
        if isinstance(detail, str) and len(detail) > _MAX_DETAIL:
            detail = detail[: _MAX_DETAIL - 1] + "…"
        stored: dict[str, Any] = {
            "step": step,
            "status": event.get("status", "ok"),
            "turn_id": turn_id,
            "ts": int(time.time() * 1000),
        }
        if "tool" in event:
            stored["tool"] = event["tool"]
        if detail is not None:
            stored["detail"] = detail
        with self._lock:
            self._events.append(stored)
        if self._store is not None:
            # Best-effort by contract: ``append`` owns its own failures and
            # returns False rather than raising, because we are standing in the
            # middle of a turn and history is never worth an answer.
            self._store.append(stored)
        log.info(
            "step=%s tool=%s status=%s detail=%s turn=%s",
            stored.get("step"),
            stored.get("tool"),
            stored.get("status"),
            stored.get("detail"),
            turn_id,
        )
        return stored

    def recent(
        self, turn_id: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> list[dict[str, Any]]:
        """Up to ``limit`` most-recent IN-MEMORY events, optionally one turn's.

        The live-turn fast path, unchanged. ``query`` is the one that reaches
        back past this process's start.
        """
        return self._from_ring(turn_id=turn_id, limit=_clamp_limit(limit))

    def query(
        self,
        *,
        since: int | None = None,
        turn_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """History read for ``/events``: newest ``limit`` matches, oldest-first.

        Durable-first, with the ring as both fast path and safety net:

          * if the ring already holds a full page of matches, it IS the answer —
            the ring holds the newest events, so nothing older could displace
            them, and we skip SQLite entirely;
          * otherwise the store answers, because it also holds the turns from
            before this process started;
          * if there is no store, or its read failed, the ring answers anyway.

        ``since`` is an inclusive epoch-ms floor (see ``EventStore.query``).
        """
        limit = _clamp_limit(limit)
        ring = self._from_ring(since=since, turn_id=turn_id, limit=limit)
        if self._store is None or len(ring) >= limit:
            return ring
        persisted = self._store.query(since=since, turn_id=turn_id, limit=limit)
        if persisted is None:
            return ring
        # An event whose write failed still lives in the ring, so prefer
        # whichever view of this window is the more complete one.
        return persisted if len(persisted) >= len(ring) else ring

    def close(self) -> None:
        """Release the durable store, if any. Safe to call more than once."""
        store, self._store = self._store, None
        if store is not None:
            store.close()

    def _from_ring(
        self,
        *,
        since: int | None = None,
        turn_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Filter the in-memory ring. Returns copies — the ring stays private."""
        with self._lock:
            items = list(self._events)
        if turn_id is not None:
            items = [e for e in items if e.get("turn_id") == turn_id]
        if since is not None:
            items = [e for e in items if e.get("ts", 0) >= since]
        return [dict(e) for e in items[-limit:]]
