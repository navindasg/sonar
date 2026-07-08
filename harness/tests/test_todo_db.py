"""todo_add (write) + state_read(kind='todos') (read): the assistant's OWN
SQLite to-do list — separate from the vault checkboxes read by todo_list."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sonar_harness.state import State
from sonar_harness.tools.base import ToolContext
from sonar_harness.tools.state_read import StateReadTool
from sonar_harness.tools.todo_add import TodoAddTool
from sonar_harness.tools.todo_done import TodoDoneTool


def _state(tmp_path):
    # Real schema (state/schema.sql) so the todos table + indexes are exercised.
    return State.open(db_path=tmp_path / "s.sqlite")


def _ctx(state):
    events: list[dict] = []
    return ToolContext(turn_id="t", state=state, emit=events.append), events


def test_add_persists_and_readback(tmp_path):
    state = _state(tmp_path)
    add = TodoAddTool()
    read = StateReadTool()

    ctx, events = _ctx(state)
    out = add.run({"text": "email the recruiter", "due": "2026-07-10"}, ctx)
    assert out.startswith("Saved to your list (#1):")
    assert events[-1]["tool"] == "todo_add" and "saved todo #1" in events[-1]["detail"]

    add.run({"text": "book flights"}, _ctx(state)[0])
    todos = json.loads(read.run({"kind": "todos"}, _ctx(state)[0]))
    # dated task sorts before the undated one
    assert [t["text"] for t in todos] == ["email the recruiter", "book flights"]
    assert todos[0]["due"] == "2026-07-10" and todos[1]["due"] is None
    state.close()


def test_add_rejects_empty_and_bad_due(tmp_path):
    state = _state(tmp_path)
    add = TodoAddTool()
    assert add.run({"text": "  "}, _ctx(state)[0]).startswith("error:")
    assert add.run({"text": "x", "due": "July 10"}, _ctx(state)[0]).startswith("error:")
    # nothing was written -> empty read returns the friendly message, not JSON
    assert StateReadTool().run({"kind": "todos"}, _ctx(state)[0]) == "No todos rows yet."
    state.close()


def test_state_read_rejects_unknown_kind(tmp_path):
    state = _state(tmp_path)
    assert StateReadTool().run({"kind": "nope"}, _ctx(state)[0]).startswith("error:")
    state.close()


def test_todo_done_by_id_and_by_text(tmp_path):
    state = _state(tmp_path)
    add, done, read = TodoAddTool(), TodoDoneTool(), StateReadTool()
    add.run({"text": "email the recruiter"}, _ctx(state)[0])  # #1
    add.run({"text": "renew my passport"}, _ctx(state)[0])     # #2

    assert done.run({"id": 1}, _ctx(state)[0]).startswith("Marked done (#1):")
    # done items drop out of the open read
    remaining = json.loads(read.run({"kind": "todos"}, _ctx(state)[0]))
    assert [t["text"] for t in remaining] == ["renew my passport"]
    # already-done and not-found are handled, not crashes
    assert "already done" in done.run({"id": 1}, _ctx(state)[0])
    assert done.run({"id": 99}, _ctx(state)[0]).startswith("error:")
    # unique text fragment resolves; id path takes precedence otherwise
    assert done.run({"text": "passport"}, _ctx(state)[0]).startswith("Marked done (#2):")
    state.close()


def test_todo_done_text_ambiguous_asks_for_id(tmp_path):
    state = _state(tmp_path)
    add, done = TodoAddTool(), TodoDoneTool()
    add.run({"text": "call the bank about the mortgage"}, _ctx(state)[0])
    add.run({"text": "call the dentist"}, _ctx(state)[0])
    msg = done.run({"text": "call"}, _ctx(state)[0])
    assert msg.startswith("error:") and "#1" in msg and "#2" in msg
    state.close()


def test_expiry_undated_expires_dated_survives_until_due(tmp_path):
    state = _state(tmp_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(hours=30)).isoformat()   # >24h ago
    recent = (now - timedelta(hours=2)).isoformat()  # <24h ago
    conn = state.conn
    conn.execute("INSERT INTO todos (id, created_at, text, status, due) VALUES (1,?, 'stale undated','open',NULL)", (old,))
    conn.execute("INSERT INTO todos (id, created_at, text, status, due) VALUES (2,?, 'fresh undated','open',NULL)", (recent,))
    conn.execute("INSERT INTO todos (id, created_at, text, status, due) VALUES (3,?, 'due yesterday','open','2026-07-07')", (old,))
    conn.execute("INSERT INTO todos (id, created_at, text, status, due) VALUES (4,?, 'due today','open','2026-07-08')", (old,))
    conn.execute("INSERT INTO todos (id, created_at, text, status, due) VALUES (5,?, 'due next week','open','2026-07-15')", (old,))
    conn.execute("INSERT INTO todos (id, created_at, text, status, done_at) VALUES (6,?, 'old done','done',?)", (old, old))
    conn.commit()

    removed = state.expire_todos(now=now)
    survivors = {r["id"] for r in conn.execute("SELECT id FROM todos").fetchall()}
    assert removed == 3  # #1 stale-undated, #3 due-yesterday, #6 old-done
    assert survivors == {2, 4, 5}  # fresh undated, due today (through the day), future due
    state.close()
