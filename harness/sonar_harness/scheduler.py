"""In-harness catch-up scheduler for time-of-day jobs (morning brief, nightly
note-formatter).

Why poll instead of launchd's StartCalendarInterval: a calendar job fires at an
instant and launchd catches up a run missed while ASLEEP — but a run missed
while the Mac was POWERED OFF is skipped entirely. This scheduler POLLS: on
every tick it runs each job that is "due today and not yet done". The harness is
a durable launchd agent that starts at login, so its first tick after boot
catches up anything missed while the machine was off — "whenever we're able to,
it fires".

Each job is idempotent via a done-marker (the brief's dated vault note; a
per-day marker file for the formatter), so re-ticking never double-runs. Jobs
run as subprocesses in a daemon thread, so a job's LLM/vault work never blocks
the server's request loop. Failures are isolated and logged: one job failing
(or Ollama being down) never aborts the others or the loop — the work just stays
"not done" and the next tick retries. This mirrors the formatter's own queue,
which already "survives sleep and failures".
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Callable, Sequence

from sonar_harness.ollama_client import DEFAULT_OLLAMA_URL

log = logging.getLogger("sonar.scheduler")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHED_STATE = Path(os.environ.get("SONAR_HOME", str(Path.home() / ".sonar"))) / "scheduler"

# The vendored obsidian_rag formatter reads its config from here by default (the
# CLI's own default). Sonar OWNS this file: it generates a working one when it's
# missing (see ensure_formatter_config) so the formatter is self-contained on a
# fresh install. Override the location with SONAR_FORMATTER_CONFIG.
_DEFAULT_FORMATTER_CONFIG = Path("~/.obsidian-rag/config.yaml")

# A minimal, WORKING obsidian_rag config — vault + daily_format enabled — unlike
# the vendored DEFAULT_CONFIG (placeholder vault, formatter commented out, which
# makes format-daily exit "enable daily_format"). Only the vault and Ollama URL
# are filled from Sonar's own settings; everything else takes obsidian_rag's
# defaults (model=null → auto-select). Kept as a plain template so the harness
# needs no YAML dependency to write it.
_FORMATTER_CONFIG_TEMPLATE = """\
# obsidian-rag configuration — generated and MANAGED BY SONAR.
#
# Sonar's in-harness scheduler drives the daily-note formatter against Sonar's
# own vendored obsidian_rag (see harness/sonar_harness/scheduler.py), so this
# file exists to make that formatter self-contained: no manual setup on a fresh
# install. Sonar only CREATES this file when it is missing — it never overwrites
# it — so anything you add below (e.g. a blacklist) is preserved.

vaults:
  - name: {vault_name}
    path: {vault_path}

embedding:
  ollama_url: {ollama_url}

daily_format:
  enabled: true
  model: null              # auto-select from pulled models (gemma4:26b-mlx first)
  # blacklist: dates (YYYY-MM-DD) of hand-formatted notes to leave untouched.
  blacklist: []
  format_tag: "#!format"   # type this in any note to queue it for formatting
"""


@dataclass(frozen=True)
class Job:
    """One scheduled unit of work.

    due:  is it time (today) to run — e.g. ``now >= 08:00``.
    done: has it already completed for its current period (idempotency)?
    run:  do the work; blocking. Raising is caught and logged by ``tick``.
    """

    name: str
    due: Callable[[datetime], bool]
    done: Callable[[datetime], bool]
    run: Callable[[], None]


def tick(jobs: Sequence[Job], now: datetime) -> list[str]:
    """Run every job that is due and not yet done. Idempotent — safe to call as
    often as you like. Returns the names of jobs run this tick. A job that raises
    is logged and skipped; it stays "not done", so the next tick retries it."""
    ran: list[str] = []
    for job in jobs:
        try:
            if not job.due(now) or job.done(now):
                continue
            log.info("scheduler: running %s", job.name)
            job.run()
            ran.append(job.name)
            log.info("scheduler: %s complete", job.name)
        except Exception:  # noqa: BLE001 — isolate one job's failure from the rest
            log.exception("scheduler: job %r failed; will retry next tick", job.name)
    return ran


def run_forever(
    jobs: Sequence[Job],
    *,
    interval_s: float,
    stop: threading.Event,
    clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
) -> None:
    """Tick immediately (catch-up on start), then every ``interval_s`` until
    ``stop`` is set. ``clock`` returns LOCAL time so 'due at 08:00' means the
    user's 08:00."""
    while not stop.is_set():
        tick(jobs, clock())
        stop.wait(interval_s)


class Scheduler:
    """Owns the daemon thread running the tick loop."""

    def __init__(self, jobs: Sequence[Job], *, interval_s: float) -> None:
        self._jobs = list(jobs)
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=run_forever,
            args=(self._jobs,),
            kwargs={"interval_s": self._interval_s, "stop": self._stop},
            name="sonar-scheduler",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "scheduler started: %d job(s), tick every %.0fs", len(self._jobs), self._interval_s
        )

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)


# ---- subprocess helper -------------------------------------------------------
def _run(cmd: list[str], *, cwd: Path, timeout: float) -> None:
    """Run a job command; raise on failure so ``tick`` logs it and retries."""
    log.info("scheduler: exec %s", " ".join(cmd))
    result = subprocess.run(  # noqa: S603 — fixed commands, not user input
        cmd, cwd=str(cwd), timeout=timeout, capture_output=True, text=True
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-500:]
        raise RuntimeError(f"{cmd[0]} exited {result.returncode}: {tail}")


# ---- job factories -----------------------------------------------------------
def _minutes(t: datetime | dtime) -> int:
    return t.hour * 60 + t.minute


def brief_job(
    *, repo_root: Path, vault_path: Path, hour: int, minute: int, timeout: float = 300.0
) -> Job:
    """Morning brief: due at/after HH:MM local; done once today's vault note
    exists (the artifact ``morning_brief.py`` writes). Runs the same proven
    ``sonar.sh brief`` the launchd agent used."""
    target = dtime(hour, minute)

    def due(now: datetime) -> bool:
        return _minutes(now) >= _minutes(target)

    def done(now: datetime) -> bool:
        return (vault_path / "Sonar" / "Brief" / f"{now.date().isoformat()}.md").exists()

    def run() -> None:
        _run(["/bin/bash", str(repo_root / "scripts" / "sonar.sh"), "brief"],
             cwd=repo_root, timeout=timeout)

    return Job("brief", due, done, run)


def formatter_daily_job(
    *, python: Path, repo_root: Path, config: Path | None = None, timeout: float = 900.0
) -> Job:
    """Nightly daily-note formatter (full run) — Sonar's own vendored
    ``obsidian_rag``, run as an isolated subprocess (fire-and-forget: it rewrites
    notes but surfaces NOTHING back to the harness). Due any time — the runner's
    own persistent queue catches up every unformatted note — but gated to once
    per local day by a marker file so it isn't re-run on every tick. Runs against
    the Sonar-managed config (``--config``) so it needs no external setup."""
    cfg = config or _formatter_config_path()

    def marker(d: date) -> Path:
        return _SCHED_STATE / f"formatter-{d.isoformat()}.done"

    def due(_now: datetime) -> bool:
        return True

    def done(now: datetime) -> bool:
        return marker(now.date()).exists()

    def run() -> None:
        _run([str(python), "-m", "obsidian_rag", "--verbose", "format-daily",
              "--config", str(cfg)], cwd=repo_root, timeout=timeout)
        _SCHED_STATE.mkdir(parents=True, exist_ok=True)
        marker(datetime.now().astimezone().date()).touch()

    return Job("formatter-daily", due, done, run)


def formatter_tags_job(
    *, python: Path, repo_root: Path, config: Path | None = None, timeout: float = 300.0
) -> Job:
    """Format-tag poll: pick up notes opted-in via the format tag. A poll, so it
    runs every tick (no done-gate); the runner no-ops when nothing is tagged."""
    cfg = config or _formatter_config_path()

    def due(_now: datetime) -> bool:
        return True

    def done(_now: datetime) -> bool:
        return False

    def run() -> None:
        _run([str(python), "-m", "obsidian_rag", "--verbose", "format-daily",
              "--tags-only", "--config", str(cfg)], cwd=repo_root, timeout=timeout)

    return Job("formatter-tags", due, done, run)


def _formatter_python() -> Path:
    """Interpreter that runs the vendored ``obsidian_rag`` formatter. Defaults to
    the harness's OWN venv — ``obsidian_rag`` is a harness dependency (see
    ``harness/pyproject.toml``: ``obsidian-rag = {path = "../rag"}``), so the
    formatter runs from Sonar's own code with no external install. Overridable
    via SONAR_FORMATTER_PYTHON (tests / packaging)."""
    override = os.environ.get("SONAR_FORMATTER_PYTHON")
    return Path(override) if override else Path(sys.executable)


def _formatter_config_path() -> Path:
    """Where the Sonar-managed formatter config lives. Defaults to obsidian_rag's
    own default location; override with SONAR_FORMATTER_CONFIG."""
    return Path(
        os.environ.get("SONAR_FORMATTER_CONFIG", str(_DEFAULT_FORMATTER_CONFIG))
    ).expanduser()


def ensure_formatter_config(
    path: Path, *, vault_path: str | Path, vault_name: str, ollama_url: str
) -> Path:
    """Make the formatter self-contained: write a WORKING obsidian_rag config at
    ``path`` if none exists yet, filled from Sonar's own vault/Ollama settings.

    CREATE-ONLY — it never overwrites an existing file, so a user's own config
    (and any hand-formatted-note blacklist in it) is always preserved. On a fresh
    machine this is what lets the scheduled formatter run with zero manual setup.
    Returns ``path`` either way."""
    if path.exists():
        return path
    content = _FORMATTER_CONFIG_TEMPLATE.format(
        vault_name=vault_name, vault_path=str(vault_path), ollama_url=ollama_url
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)  # atomic: never leave a half-written config
    log.info("scheduler: generated Sonar-managed formatter config at %s", path)
    return path


def default_jobs(*, vault_path: str | Path, repo_root: Path = _REPO_ROOT) -> list[Job]:
    """The jobs the harness manages: the brief (surfaced) plus the note-formatter
    (fire-and-forget). The formatter is Sonar's own vendored ``obsidian_rag``,
    always scheduled — it's a required harness dependency."""
    hour = int(os.environ.get("SONAR_BRIEF_HOUR", "8"))
    minute = int(os.environ.get("SONAR_BRIEF_MIN", "0"))
    python = _formatter_python()
    config = _formatter_config_path()
    return [
        brief_job(repo_root=repo_root, vault_path=Path(vault_path), hour=hour, minute=minute),
        formatter_daily_job(python=python, repo_root=repo_root, config=config),
        formatter_tags_job(python=python, repo_root=repo_root, config=config),
    ]


def start_scheduler(*, vault_path: str | Path) -> Scheduler | None:
    """Build + start the harness scheduler, or return None if disabled via
    SONAR_SCHED_ENABLED=0. Also generates the Sonar-managed formatter config on
    first run so the formatter is self-contained (create-only; never clobbers)."""
    if os.environ.get("SONAR_SCHED_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
        log.info("scheduler disabled (SONAR_SCHED_ENABLED)")
        return None
    ensure_formatter_config(
        _formatter_config_path(),
        vault_path=vault_path,
        vault_name=os.environ.get("SONAR_VAULT_NAME", "sonar"),
        ollama_url=os.environ.get("SONAR_OLLAMA_URL", DEFAULT_OLLAMA_URL),
    )
    interval = float(os.environ.get("SONAR_SCHED_INTERVAL_S", "300"))
    scheduler = Scheduler(default_jobs(vault_path=vault_path), interval_s=interval)
    scheduler.start()
    return scheduler
