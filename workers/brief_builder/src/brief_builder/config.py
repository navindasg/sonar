"""Configuration: environment variables + defaults.

Immutable config object built once from the environment (and optional CLI
overrides). No secrets live here — the Ollama endpoint is a localhost URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

# ---- Defaults (overridable via env or CLI) -------------------------------

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL_FAST = "gemma4:e4b-mlx"
DEFAULT_VAULT = "~/Documents/Obsidian Vault"  # note: has a space (see DECISIONS.md)
DEFAULT_DB = "~/.config/sonar/state/sonar.db"

# The worker only EVER writes under <vault>/<OUTPUT_SUBDIR>. Everything else in
# the vault is treated as read-only user content.
OUTPUT_SUBDIR = Path("Sonar") / "Briefs"

# The output subdir is excluded from input gathering so briefs never feed on
# themselves.
EXCLUDED_TOP_DIR = "Sonar"

WORKER_NAME = "brief-builder"

# Bounds — keep the worker's work (and the LLM prompt) small and deterministic.
DEFAULT_MAX_NOTES = 8
DEFAULT_MAX_CHARS_PER_NOTE = 600
DEFAULT_LLM_TIMEOUT_S = 120.0
VALID_WINDOWS = ("morning", "any")


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration for one worker invocation."""

    ollama_host: str
    model_fast: str
    vault: Path
    db_path: Path
    window: str
    dry_run: bool
    max_notes: int
    max_chars_per_note: int
    llm_timeout_s: float

    def with_overrides(self, **changes: object) -> "Config":
        """Return a new Config with the given fields replaced (never mutate)."""
        return replace(self, **changes)


def _expand(path: str) -> Path:
    """Expand ~ and environment variables to an absolute Path."""
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def load_config(
    *,
    window: str = "any",
    vault: str | None = None,
    db_path: str | None = None,
    dry_run: bool = False,
) -> Config:
    """Build a Config from env vars + explicit overrides.

    Explicit (CLI) arguments win over environment variables, which win over
    the module defaults.

    Raises:
        ValueError: if `window` is not one of VALID_WINDOWS.
    """
    if window not in VALID_WINDOWS:
        raise ValueError(
            f"invalid window {window!r}; expected one of {VALID_WINDOWS}"
        )

    ollama_host = _env("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
    model_fast = _env("MODEL_FAST", DEFAULT_MODEL_FAST)
    vault_raw = vault if vault is not None else _env("SONAR_VAULT", DEFAULT_VAULT)
    db_raw = db_path if db_path is not None else _env("SONAR_DB", DEFAULT_DB)

    return Config(
        ollama_host=ollama_host.rstrip("/"),
        model_fast=model_fast,
        vault=_expand(vault_raw),
        db_path=_expand(db_raw),
        window=window,
        dry_run=dry_run,
        max_notes=DEFAULT_MAX_NOTES,
        max_chars_per_note=DEFAULT_MAX_CHARS_PER_NOTE,
        llm_timeout_s=DEFAULT_LLM_TIMEOUT_S,
    )
