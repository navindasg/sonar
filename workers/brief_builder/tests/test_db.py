"""Tests for db init + parameterized inserts against a temp DB file."""

from __future__ import annotations

from pathlib import Path

from brief_builder import db as dbmod


def test_init_db_creates_tables_and_wal(temp_db: Path) -> None:
    conn = dbmod.init_db(temp_db)
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"briefs", "worker_runs"} <= tables

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()
    assert temp_db.exists()


def test_init_db_is_idempotent(temp_db: Path) -> None:
    dbmod.init_db(temp_db).close()
    # Second init must not raise and must not duplicate/lose data.
    conn = dbmod.init_db(temp_db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_insert_brief_roundtrip(temp_db: Path) -> None:
    conn = dbmod.init_db(temp_db)
    try:
        brief_id = dbmod.insert_brief(
            conn,
            created_at="2026-07-06T12:00:00+00:00",
            window="any",
            title="Any Brief — 2026-07-06",
            body_md="- did a thing",
            note_path="/tmp/x/Sonar/Briefs/2026-07-06-any.md",
        )
        assert brief_id == 1
        row = conn.execute(
            "SELECT * FROM briefs WHERE id = ?", (brief_id,)
        ).fetchone()
        assert row["window"] == "any"
        assert row["title"].startswith("Any Brief")
        assert row["body_md"] == "- did a thing"
        assert row["note_path"].endswith("2026-07-06-any.md")
    finally:
        conn.close()


def test_worker_run_lifecycle(temp_db: Path) -> None:
    conn = dbmod.init_db(temp_db)
    try:
        run_id = dbmod.start_worker_run(
            conn, "brief-builder", "2026-07-06T12:00:00+00:00"
        )
        running = conn.execute(
            "SELECT status, finished_at FROM worker_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert running["status"] == "running"
        assert running["finished_at"] is None

        dbmod.finish_worker_run(
            conn, run_id, "2026-07-06T12:00:05+00:00", "ok", detail="brief_id=1"
        )
        done = conn.execute(
            "SELECT status, finished_at, detail FROM worker_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert done["status"] == "ok"
        assert done["finished_at"].endswith(":05+00:00")
        assert done["detail"] == "brief_id=1"
    finally:
        conn.close()


def test_insert_brief_allows_null_note_path(temp_db: Path) -> None:
    conn = dbmod.init_db(temp_db)
    try:
        brief_id = dbmod.insert_brief(
            conn,
            created_at="2026-07-06T12:00:00+00:00",
            window="morning",
            title="t",
            body_md="b",
            note_path=None,
        )
        row = conn.execute(
            "SELECT note_path FROM briefs WHERE id = ?", (brief_id,)
        ).fetchone()
        assert row["note_path"] is None
    finally:
        conn.close()
