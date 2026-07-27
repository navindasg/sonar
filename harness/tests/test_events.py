"""Durable step-event history: persistence, filters, retention, degradation.

``GET /events`` is polled today by the overlay bridge and voice loop, and is
about to back a Console/audit window ("what did Sonar do and why"). So these
tests pin two things at once: that history now survives a harness restart, and
that the wire shape those existing pollers already parse did not move.

Everything here is PULL-only — nothing in this module starts a timer, a thread
or a daemon; the store is swept lazily, on the caller's thread.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from sonar_harness import event_store
from sonar_harness.event_store import (
    DEFAULT_RETAIN_DAYS,
    MS_PER_DAY,
    EventStore,
    retain_days,
)
from sonar_harness.events import EventSink
from sonar_harness.server import get_events

_DB = "events.sqlite"


def _store(tmp_path, name: str = _DB) -> EventStore:
    """A store over a real schema-initialized DB (state/schema.sql, not a stub)."""
    return EventStore.open(db_path=tmp_path / name)


def _sink(tmp_path, name: str = _DB, **kwargs) -> EventSink:
    return EventSink(store=_store(tmp_path, name), **kwargs)


def _seed(store: EventStore, rows: list[tuple[str, int, str]]) -> None:
    """Insert events with PINNED timestamps — ``record`` stamps wall-clock ms,
    which is too coarse to tell two events in a tight loop apart."""
    for turn_id, ts, step in rows:
        store.append({"turn_id": turn_id, "ts": ts, "step": step, "status": "ok"})


def _get_events(sink: EventSink, **params) -> dict:
    """Call the /events route function directly (no lifespan, no RAG build).

    Plain call, not ``asyncio.run``: the handler is sync on purpose so Starlette
    runs its blocking store read in the threadpool instead of on the loop.
    """
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(events=sink)))
    return json.loads(get_events(request, **params).body)


# ---- persistence -------------------------------------------------------------
def test_history_survives_a_fresh_sink(tmp_path):
    first = _sink(tmp_path)
    first.record("turn-a", {"step": "turn_start", "detail": "what's on my plate"})
    first.record("turn-a", {"step": "tool", "tool": "daily.brief", "status": "ok"})
    first.close()

    # A brand-new process would look exactly like this: cold ring, same DB.
    second = _sink(tmp_path)
    assert second.recent() == []
    rows = second.query()
    assert [r["step"] for r in rows] == ["turn_start", "tool"]
    assert rows[0]["turn_id"] == "turn-a"
    assert rows[1]["tool"] == "daily.brief"
    second.close()


def test_persisted_events_match_the_in_memory_shape(tmp_path):
    sink = _sink(tmp_path)
    stored = sink.record("t1", {"step": "final", "detail": "done"})
    assert sink.recent() == [stored]

    reader = _sink(tmp_path)  # empty ring: this answer can only come from SQLite
    (row,) = reader.query()
    assert row == stored
    # Absent optional keys stay ABSENT rather than null — the pollers were
    # written against a ring that simply never sets them.
    assert "tool" not in row
    sink.close()
    reader.close()


def test_long_detail_is_truncated_before_it_is_persisted(tmp_path):
    sink = _sink(tmp_path)
    sink.record("t", {"step": "tool", "tool": "web.search", "detail": "x" * 500})
    sink.close()

    (row,) = _sink(tmp_path).query()
    assert len(row["detail"]) == 120 and row["detail"].endswith("…")


def test_sink_without_a_store_is_pure_memory(tmp_path):
    sink = EventSink(maxlen=2)
    sink.record("t", {"step": "turn_start"})
    sink.record("t", {"step": "tool", "tool": "a"})
    sink.record("t", {"step": "final"})
    assert [e["step"] for e in sink.query()] == ["tool", "final"]  # oldest evicted
    assert sink.recent(turn_id="other") == []


# ---- filters + ordering ------------------------------------------------------
def test_query_filters_by_turn_id(tmp_path):
    store = _store(tmp_path)
    _seed(store, [("a", 100, "turn_start"), ("b", 200, "turn_start"), ("a", 300, "final")])
    assert [r["ts"] for r in store.query(turn_id="a")] == [100, 300]
    assert store.query(turn_id="nope") == []
    store.close()


def test_query_since_is_inclusive_and_oldest_first(tmp_path):
    store = _store(tmp_path)
    _seed(store, [("t", 100, "turn_start"), ("t", 200, "tool"), ("t", 300, "final")])
    assert [r["ts"] for r in store.query(since=200)] == [200, 300]
    assert [r["ts"] for r in store.query()] == [100, 200, 300]
    store.close()


def test_query_limit_keeps_the_newest_and_ordering_stays_ascending(tmp_path):
    store = _store(tmp_path)
    _seed(store, [("t", ts, "tool") for ts in (100, 200, 300, 400)])
    assert [r["ts"] for r in store.query(limit=2)] == [300, 400]
    store.close()


def test_ordering_is_stable_for_identical_timestamps(tmp_path):
    store = _store(tmp_path)
    steps = ["turn_start", "tool", "tool_result_summary", "final"]
    _seed(store, [("t", 1000, step) for step in steps])
    assert [r["step"] for r in store.query()] == steps
    store.close()


def test_since_and_turn_id_combine(tmp_path):
    store = _store(tmp_path)
    _seed(store, [("a", 100, "tool"), ("b", 200, "tool"), ("a", 300, "final")])
    assert [r["ts"] for r in store.query(since=150, turn_id="a")] == [300]
    store.close()


# ---- retention ---------------------------------------------------------------
def test_prune_drops_events_older_than_the_retention_window(tmp_path):
    store = _store(tmp_path)
    now = 60 * MS_PER_DAY  # far enough in that a 30-day window has room behind it
    _seed(
        store,
        [
            ("stale", now - 31 * MS_PER_DAY, "final"),
            ("edge", now - 29 * MS_PER_DAY, "final"),
            ("fresh", now, "final"),
        ],
    )
    assert store.prune(now_ms=now) == 1
    assert [r["turn_id"] for r in store.query()] == ["edge", "fresh"]
    store.close()


def test_retention_window_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SONAR_EVENTS_RETAIN_DAYS", "2")
    store = _store(tmp_path)
    now = 10 * MS_PER_DAY
    _seed(store, [("old", now - 3 * MS_PER_DAY, "final"), ("new", now - MS_PER_DAY, "final")])
    assert store.prune(now_ms=now) == 1
    assert [r["turn_id"] for r in store.query()] == ["new"]
    store.close()


def test_unusable_retention_env_falls_back_to_the_default(monkeypatch):
    for bad in ("not-a-number", "0", "-5", ""):
        monkeypatch.setenv("SONAR_EVENTS_RETAIN_DAYS", bad)
        assert retain_days() == DEFAULT_RETAIN_DAYS
    monkeypatch.delenv("SONAR_EVENTS_RETAIN_DAYS")
    assert retain_days() == DEFAULT_RETAIN_DAYS


# ---- graceful degradation ----------------------------------------------------
def test_a_broken_db_never_raises_into_the_turn(tmp_path):
    store = _store(tmp_path)
    store.close()  # the DB goes away mid-turn
    sink = EventSink(store=store)

    stored = sink.record("t", {"step": "tool", "tool": "search", "status": "ok"})
    assert stored["step"] == "tool"  # the turn's event still happened...
    assert sink.recent() == [stored]  # ...is still in the ring...
    assert sink.query() == [stored]  # ...and the read degrades to the ring too


def test_try_open_returns_none_when_the_db_cannot_be_opened(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    assert EventStore.try_open(db_path=blocker / "sonar.sqlite") is None


def test_append_reports_failure_without_raising(tmp_path):
    store = _store(tmp_path)
    assert store.append({"turn_id": "t", "ts": 1, "step": "final", "status": "ok"}) is True
    store.close()
    assert store.append({"turn_id": "t", "ts": 2, "step": "final", "status": "ok"}) is False
    assert store.query() is None  # a failed read is None, not a silent empty list
    assert store.prune(now_ms=0) == 0


def test_a_contended_db_fails_fast_instead_of_stalling_the_turn(tmp_path):
    """Losing an event is fine. Parking a live answer behind a lock is not.

    Appends run INLINE on the turn's thread, and SQLite allows exactly one
    writer — so on sqlite3's default 5-second busy timeout an unrelated writer
    (the harness's own ``State`` handle expiring todos, a worker) would hold a
    turn open for seconds. The store buys a much shorter wait: contention costs
    history, and the turn keeps moving.
    """
    store = _store(tmp_path)
    blocker = sqlite3.connect(str(tmp_path / _DB), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # takes the one write lock, holds it
    try:
        started = time.monotonic()
        landed = store.append({"turn_id": "t", "ts": 1, "step": "final", "status": "ok"})
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()
        store.close()
    assert landed is False
    assert elapsed < 1.0, f"a contended append blocked the turn for {elapsed:.1f}s"


class _DeleteAlwaysLocked:
    """A connection stand-in whose sweeps always lose the write lock."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.sweeps = 0

    def execute(self, sql: str, *args):
        if sql.lstrip().upper().startswith("DELETE"):
            self.sweeps += 1
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()


def test_a_failed_sweep_waits_for_the_next_window(tmp_path, monkeypatch):
    """One sweep per window even when sweeps fail — never one per append.

    A sweep that loses the lock and does not clear its counter leaves every
    later append "due", so each one pays the busy-timeout wait inline. Missing
    a retention window costs nothing; paying that wait per step costs the turn.
    """
    monkeypatch.setattr(event_store, "_PRUNE_EVERY", 2)
    store = _store(tmp_path)
    flaky = _DeleteAlwaysLocked(store.conn)
    store.conn = flaky

    for ts in range(1, 5):
        assert store.append({"turn_id": "t", "ts": ts, "step": "final", "status": "ok"})
    store.close()

    assert flaky.sweeps == 2  # windows [1,2] and [3,4], not appends 2,3,4


def test_writes_are_configured_not_to_stall_the_turn_thread(tmp_path):
    """The two per-connection pragmas the guarantee above rests on.

    Asserted directly because both are silent when removed: nothing fails, the
    turn loop just quietly gets slower — the exact regression the contention
    test above can only catch in its own narrow scenario.
    """
    store = _store(tmp_path)
    try:
        busy_ms = store.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = store.conn.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        store.close()
    assert 0 < busy_ms <= 500
    assert synchronous == 1  # NORMAL: WAL-safe, and no fsync per step-event


# ---- the /events endpoint ----------------------------------------------------
class _SlowStore:
    """A store whose read takes a while — a held write lock, or a live sweep."""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    def append(self, event):
        return True

    def query(self, *, since=None, turn_id=None, limit=100):
        time.sleep(self._delay)
        return []

    def close(self) -> None:
        return None


class _Heartbeat:
    """Measures the longest the event loop went without running a ready task."""

    def __init__(self, tick: float = 0.01) -> None:
        self._tick = tick
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> float:
        worst, last = 0.0, time.monotonic()
        while not self._stopped:
            await asyncio.sleep(self._tick)
            now = time.monotonic()
            worst = max(worst, now - last - self._tick)
            last = now
        return worst


def test_endpoint_response_shape_is_unchanged(tmp_path):
    sink = _sink(tmp_path)
    sink.record("t1", {"step": "tool", "tool": "search", "detail": "wsn", "status": "ok"})
    body = _get_events(sink)
    assert set(body) == {"events"}
    (event,) = body["events"]
    assert event == {
        "step": "tool",
        "status": "ok",
        "turn_id": "t1",
        "ts": event["ts"],
        "tool": "search",
        "detail": "wsn",
    }
    sink.close()


def test_endpoint_serves_turns_from_before_this_process(tmp_path):
    previous = _sink(tmp_path)
    previous.record("old-turn", {"step": "final", "detail": "answered yesterday"})
    previous.close()

    # The overlay's existing call — turn_id only — now reaches durable history.
    body = _get_events(_sink(tmp_path), turn_id="old-turn")
    assert [e["detail"] for e in body["events"]] == ["answered yesterday"]


def test_endpoint_filters_by_since_and_turn_id(tmp_path):
    sink = _sink(tmp_path)
    sink.record("t1", {"step": "turn_start"})
    second = sink.record("t2", {"step": "final"})

    assert [e["turn_id"] for e in _get_events(sink, turn_id="t2")["events"]] == ["t2"]
    # record() stamps real wall-clock ms, so two events can share one — assert
    # the contract (nothing older than `since`) rather than an exact list.
    assert all(e["ts"] >= second["ts"] for e in _get_events(sink, since=second["ts"])["events"])
    assert _get_events(sink, since=second["ts"] + 1)["events"] == []
    sink.close()


def test_the_endpoint_does_not_freeze_the_event_loop(tmp_path):
    """The durable read must run OFF the loop that is streaming the answer.

    ``/events`` used to be a pure in-memory slice, so awaiting it inline cost
    nothing. It now reaches SQLite behind ``EventStore``'s lock — a lock the
    turn thread holds while it appends (up to the busy timeout) and while a
    retention sweep runs. Held on the event loop, that freezes everything the
    loop is doing, including the SSE deltas of the answer the user is waiting
    on: the exact stall the store's own busy-timeout tuning exists to prevent.
    """
    contended = 0.4  # stands in for a held write lock / a retention sweep
    sink = EventSink(store=_SlowStore(delay=contended))
    sink.record("t", {"step": "final"})
    app = FastAPI()
    app.get("/events")(get_events)
    app.state.events = sink

    async def request_while_watching_the_loop() -> tuple[int, float]:
        heartbeat = _Heartbeat()
        watcher = asyncio.create_task(heartbeat.run())
        await asyncio.sleep(0.05)  # let the heartbeat tick before we measure it
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            response = await c.get("/events")
        heartbeat.stop()
        return response.status_code, await watcher

    status, worst_gap = asyncio.run(request_while_watching_the_loop())

    assert status == 200
    assert worst_gap < contended / 2, f"the event loop froze for {worst_gap:.2f}s"


def test_endpoint_limit_returns_the_newest_events(tmp_path):
    sink = _sink(tmp_path)
    for i in range(5):
        sink.record("t", {"step": "tool", "tool": f"tool-{i}"})
    assert [e["tool"] for e in _get_events(sink, limit=2)["events"]] == ["tool-3", "tool-4"]
    # A nonsense limit must not mean "everything".
    assert len(_get_events(sink, limit=0)["events"]) == 1
    sink.close()
