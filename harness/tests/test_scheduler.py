"""Catch-up scheduler: tick semantics (due + not-done → run, idempotent,
failure-isolated) and the brief/formatter job predicates."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

from sonar_harness import scheduler
from sonar_harness.scheduler import (
    Job,
    brief_job,
    default_jobs,
    ensure_formatter_config,
    formatter_daily_job,
    formatter_tags_job,
    start_scheduler,
    tick,
)

NOON = datetime(2026, 7, 20, 12, 0)
SEVEN_AM = datetime(2026, 7, 20, 7, 0)


def _counter_job(name: str, *, due: bool, done: bool, boom: bool = False) -> tuple[Job, list[int]]:
    calls: list[int] = []

    def run() -> None:
        calls.append(1)
        if boom:
            raise RuntimeError("job blew up")

    return Job(name, lambda _n: due, lambda _n: done, run), calls


# ---- tick --------------------------------------------------------------------
def test_tick_runs_only_due_and_not_done() -> None:
    j1, c1 = _counter_job("due-fresh", due=True, done=False)   # should run
    j2, c2 = _counter_job("not-due", due=False, done=False)    # skip: not due
    j3, c3 = _counter_job("already-done", due=True, done=True)  # skip: done
    ran = tick([j1, j2, j3], NOON)
    assert ran == ["due-fresh"]
    assert c1 == [1] and c2 == [] and c3 == []


def test_tick_is_idempotent_once_done() -> None:
    done_flag = {"v": False}
    calls: list[int] = []

    def run() -> None:
        calls.append(1)
        done_flag["v"] = True

    job = Job("once", lambda _n: True, lambda _n: done_flag["v"], run)
    assert tick([job], NOON) == ["once"]
    assert tick([job], NOON) == []          # second tick: now done
    assert calls == [1]


def test_tick_isolates_a_failing_job() -> None:
    bad, cb = _counter_job("bad", due=True, done=False, boom=True)
    good, cg = _counter_job("good", due=True, done=False)
    ran = tick([bad, good], NOON)           # bad raises, good still runs
    assert ran == ["good"]
    assert cb == [1] and cg == [1]


# ---- brief job ---------------------------------------------------------------
def test_brief_due_only_after_target(tmp_path: Path) -> None:
    job = brief_job(repo_root=tmp_path, vault_path=tmp_path, hour=8, minute=0)
    assert job.due(NOON) is True
    assert job.due(SEVEN_AM) is False


def test_brief_done_when_todays_note_exists(tmp_path: Path) -> None:
    job = brief_job(repo_root=tmp_path, vault_path=tmp_path, hour=8, minute=0)
    assert job.done(NOON) is False
    note = tmp_path / "Sonar" / "Brief" / "2026-07-20.md"
    note.parent.mkdir(parents=True)
    note.write_text("# brief", encoding="utf-8")
    assert job.done(NOON) is True
    # a note for a different day does not satisfy today
    assert job.done(datetime(2026, 7, 21, 12, 0)) is False


# ---- formatter job -----------------------------------------------------------
def test_formatter_daily_done_tracks_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "_SCHED_STATE", tmp_path / "sched")
    job = formatter_daily_job(python=Path("/usr/bin/python3"), repo_root=tmp_path)
    assert job.due(NOON) is True
    assert job.done(NOON) is False
    (tmp_path / "sched").mkdir()
    (tmp_path / "sched" / "formatter-2026-07-20.done").touch()
    assert job.done(NOON) is True


# ---- Sonar-managed formatter config -----------------------------------------
def test_ensure_formatter_config_writes_a_working_config(tmp_path: Path) -> None:
    # The generated file must be a VALID obsidian_rag config with the formatter
    # ENABLED (the vendored DEFAULT_CONFIG leaves it off, which aborts the run).
    from obsidian_rag.config import load_config

    cfg = tmp_path / "nested" / "config.yaml"
    vault = tmp_path / "vault"
    vault.mkdir()  # obsidian_rag validates that the vault path exists on disk
    out = ensure_formatter_config(
        cfg, vault_path=vault, vault_name="myvault", ollama_url="http://ollama:11434"
    )
    assert out == cfg and cfg.exists()

    app = load_config(str(cfg))  # raises SystemExit if invalid/misconfigured
    assert app.daily_format.enabled is True
    assert app.vaults[0].name == "myvault"
    assert app.vaults[0].path  # non-empty vault path
    assert app.embedding.ollama_url == "http://ollama:11434"


def test_ensure_formatter_config_never_overwrites_existing(tmp_path: Path) -> None:
    # Create-only: a user's own config (with e.g. a hand-formatted blacklist) is
    # never clobbered.
    cfg = tmp_path / "config.yaml"
    original = "vaults:\n  - name: mine\n    path: /my/vault\ndaily_format:\n  blacklist:\n    - '2026-06-10'\n"
    cfg.write_text(original, encoding="utf-8")
    ensure_formatter_config(cfg, vault_path="/other", vault_name="other", ollama_url="http://y")
    assert cfg.read_text(encoding="utf-8") == original


def test_formatter_config_path_honours_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "custom.yaml"
    monkeypatch.setenv("SONAR_FORMATTER_CONFIG", str(target))
    assert scheduler._formatter_config_path() == target


def test_formatter_jobs_pass_the_managed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both formatter jobs must invoke obsidian_rag with --config <managed path>,
    # so the run uses Sonar's config rather than whatever happens to be on disk.
    monkeypatch.setattr(scheduler, "_SCHED_STATE", tmp_path / "sched")
    cmds: list[list[str]] = []
    monkeypatch.setattr(scheduler, "_run", lambda cmd, **kw: cmds.append(cmd))

    cfg = tmp_path / "cfg.yaml"
    py = Path("/usr/bin/python3")
    formatter_daily_job(python=py, repo_root=tmp_path, config=cfg).run()
    formatter_tags_job(python=py, repo_root=tmp_path, config=cfg).run()

    assert len(cmds) == 2
    for cmd in cmds:
        assert "--config" in cmd and str(cfg) in cmd
    assert "--tags-only" in cmds[1]  # the poll variant


# ---- default_jobs / start_scheduler -----------------------------------------
def test_default_jobs_are_brief_and_formatter(tmp_path: Path) -> None:
    # Formatter is always scheduled — it's Sonar's own vendored obsidian_rag.
    names = [j.name for j in default_jobs(vault_path=tmp_path)]
    assert names == ["brief", "formatter-daily", "formatter-tags"]


def test_formatter_uses_harness_interpreter_by_default(tmp_path: Path) -> None:
    # No SONAR_FORMATTER_PYTHON override -> the running interpreter (harness venv,
    # which has obsidian_rag) is used, so no external install is needed.
    assert scheduler._formatter_python() == Path(sys.executable)


def test_start_scheduler_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SONAR_SCHED_ENABLED", "0")
    assert start_scheduler(vault_path="/tmp") is None


def test_brief_hour_configurable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SONAR_BRIEF_HOUR", "6")
    brief = next(j for j in default_jobs(vault_path=tmp_path) if j.name == "brief")
    assert brief.due(datetime(2026, 7, 20, 6, 30)) is True
    assert brief.due(datetime(2026, 7, 20, 5, 30)) is False
