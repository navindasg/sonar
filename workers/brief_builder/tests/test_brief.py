"""End-to-end orchestration tests with the LLM leaf call MOCKED.

No live model is used here. We monkeypatch the summarize() leaf so control-flow,
vault writes, and DB rows are all exercised deterministically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brief_builder import brief as briefmod
from brief_builder import db as dbmod
from brief_builder.config import load_config

from conftest import write_note


@pytest.fixture
def _mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the single LLM leaf call with a deterministic stub."""

    def fake_summarize(config, notes):  # noqa: ANN001 - test stub
        return f"- summarized {len(notes)} notes"

    monkeypatch.setattr(briefmod, "summarize", fake_summarize)


def test_real_run_writes_note_and_db(
    _mock_llm: None, temp_vault: Path, temp_db: Path
) -> None:
    write_note(temp_vault, "a.md", "# A\nalpha", mtime=2000)
    write_note(temp_vault, "b.md", "# B\nbravo", mtime=1000)

    config = load_config(
        window="any", vault=str(temp_vault), db_path=str(temp_db), dry_run=False
    )
    result = briefmod.build_brief(config)

    # Note written under Sonar/Briefs only.
    assert result.note_path is not None
    assert result.note_path.exists()
    assert result.note_path.parent == temp_vault / "Sonar" / "Briefs"
    assert "summarized 2 notes" in result.note_path.read_text(encoding="utf-8")

    # DB rows present.
    conn = dbmod.connect(temp_db)
    try:
        brief_row = conn.execute(
            "SELECT * FROM briefs WHERE id = ?", (result.brief_id,)
        ).fetchone()
        assert brief_row["window"] == "any"
        assert brief_row["note_path"] == str(result.note_path)

        run_row = conn.execute(
            "SELECT * FROM worker_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert run_row["status"] == "ok"
        assert run_row["worker"] == "brief-builder"
        assert f"brief_id={result.brief_id}" in run_row["detail"]
    finally:
        conn.close()


def test_dry_run_writes_nothing(
    _mock_llm: None, temp_vault: Path, temp_db: Path
) -> None:
    write_note(temp_vault, "a.md", "# A\nalpha", mtime=2000)

    config = load_config(
        window="morning", vault=str(temp_vault), db_path=str(temp_db), dry_run=True
    )
    result = briefmod.build_brief(config)

    assert result.dry_run is True
    assert result.note_path is None
    assert result.brief_id is None
    # No vault output dir, no DB file created.
    assert not (temp_vault / "Sonar").exists()
    assert not temp_db.exists()
    # But the composed markdown is available for the CLI to print.
    assert "summarized 1 notes" in result.markdown


def test_run_with_empty_vault(_mock_llm: None, temp_vault: Path, temp_db: Path) -> None:
    config = load_config(
        window="any", vault=str(temp_vault), db_path=str(temp_db), dry_run=False
    )
    result = briefmod.build_brief(config)
    assert result.note_path is not None and result.note_path.exists()
    # No Sources section when there were no notes.
    assert "## Sources" not in result.note_path.read_text(encoding="utf-8")


def test_llm_failure_records_error_run(
    monkeypatch: pytest.MonkeyPatch, temp_vault: Path, temp_db: Path
) -> None:
    from brief_builder.llm import LLMError

    def boom(config, notes):  # noqa: ANN001 - test stub
        raise LLMError("ollama down")

    monkeypatch.setattr(briefmod, "summarize", boom)
    config = load_config(
        window="any", vault=str(temp_vault), db_path=str(temp_db), dry_run=False
    )
    with pytest.raises(LLMError):
        briefmod.build_brief(config)

    # No vault note — the LLM fails before write_note().
    assert not (temp_vault / "Sonar").exists()

    # But the failure IS recorded: the audit row is opened before the LLM call,
    # so worker_runs captures it with status='error' and no briefs row.
    assert temp_db.exists()
    conn = dbmod.connect(temp_db)
    try:
        run_row = conn.execute(
            "SELECT * FROM worker_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert run_row is not None
        assert run_row["status"] == "error"
        assert run_row["worker"] == "brief-builder"
        assert "ollama down" in run_row["detail"]
        assert run_row["finished_at"] is not None
        assert conn.execute("SELECT COUNT(*) AS n FROM briefs").fetchone()["n"] == 0
    finally:
        conn.close()
