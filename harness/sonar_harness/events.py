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
the overlay can poll it. A later pass can swap this for the localhost
WebSocket without touching the emit call sites.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from threading import Lock
from typing import Any, Deque

log = logging.getLogger("sonar.events")

_MAX_DETAIL = 120
_VALID_STEPS = frozenset(
    {"turn_start", "tool", "tool_result_summary", "model_switch", "final"}
)


class EventSink:
    """Thread-safe bounded ring of step-events, plus a structured log line."""

    def __init__(self, maxlen: int = 512) -> None:
        self._events: Deque[dict[str, Any]] = deque(maxlen=maxlen)
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
        log.info(
            "step=%s tool=%s status=%s detail=%s turn=%s",
            stored.get("step"),
            stored.get("tool"),
            stored.get("status"),
            stored.get("detail"),
            turn_id,
        )
        return stored

    def recent(self, turn_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Return up to ``limit`` most-recent events, optionally one turn's."""
        with self._lock:
            items = list(self._events)
        if turn_id is not None:
            items = [e for e in items if e.get("turn_id") == turn_id]
        return items[-limit:]
