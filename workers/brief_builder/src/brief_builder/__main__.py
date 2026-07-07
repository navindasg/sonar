"""CLI entrypoint for the brief-builder worker.

Usage:
    python -m brief_builder --window any --vault "<path>" [--db <path>] [--dry-run]

Exit codes:
    0  success (brief written, or dry-run composed)
    1  runtime failure (LLM unreachable, unsafe path, DB error, ...)
    2  bad arguments (handled by argparse)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .brief import build_brief
from .config import VALID_WINDOWS, load_config
from .llm import LLMError
from .vault import UnsafeWritePathError


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="brief_builder",
        description="Assemble a Sonar daily brief into SQLite + an Obsidian vault note.",
    )
    parser.add_argument(
        "--window",
        choices=VALID_WINDOWS,
        default="any",
        help="Which brief window to assemble (default: any).",
    )
    parser.add_argument(
        "--vault",
        default=None,
        help="Obsidian vault root (default: $SONAR_VAULT or ~/Documents/Obsidian Vault).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite state DB path (default: $SONAR_DB or ~/.config/sonar/state/sonar.db).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose and print the brief to stdout; write NOTHING.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = _parse_args(argv)

    try:
        config = load_config(
            window=args.window,
            vault=args.vault,
            db_path=args.db,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    try:
        result = build_brief(config, now=now)
    except LLMError as exc:
        print(f"error: LLM leaf call failed: {exc}", file=sys.stderr)
        return 1
    except UnsafeWritePathError as exc:
        print(f"error: refused unsafe vault write: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: I/O failure: {exc}", file=sys.stderr)
        return 1

    if result.dry_run:
        print(f"# DRY RUN — nothing written (window={config.window})\n")
        print(result.markdown)
    else:
        print(f"wrote brief #{result.brief_id}: {result.note_path}")
        print(f"state db: {config.db_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
