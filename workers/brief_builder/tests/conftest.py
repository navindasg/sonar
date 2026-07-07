"""Shared pytest fixtures.

Everything here operates on temp dirs — tests NEVER touch the real vault
(~/Documents/Obsidian Vault) or a real state DB.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Repo root: tests/ -> brief_builder/ -> workers/ -> <repo root>.
REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"


@pytest.fixture(autouse=True)
def _point_schema_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make db.init_db resolve the repo's schema explicitly and deterministically."""
    monkeypatch.setenv("SONAR_SCHEMA", str(SCHEMA_PATH))
    # Ensure ambient env can't leak the real vault/db into a test config.
    monkeypatch.delenv("SONAR_VAULT", raising=False)
    monkeypatch.delenv("SONAR_DB", raising=False)


@pytest.fixture
def temp_vault(tmp_path: Path) -> Path:
    """An empty temp Obsidian-style vault root."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """A path (not yet created) for a temp state DB."""
    return tmp_path / "state" / "sonar-test.db"


def write_note(vault: Path, rel: str, body: str, *, mtime: float | None = None) -> Path:
    """Helper: create a markdown note at vault/rel with optional mtime."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path
