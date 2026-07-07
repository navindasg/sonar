"""Layered system-prompt assembly (ported concept from brook37 agent.py).

The system prompt is built in cache-friendly layers, most-static first:

  [0] charter   — the stable, expensive prefix (identity + tool policy). A
                  future prompt-cache breakpoint pins here; keep it verbatim
                  and unchanging across turns so it stays cacheable.
  [1] clock     — the current time, injected per turn (cheap, always changes).
  [2] context   — retrieved grounding, injected only when present.

brook37 layered these as separate Anthropic system *blocks* with a cache
breakpoint on the charter. Ollama takes a single system string, so we
concatenate — but keep the charter first and unchanged so a later swap to a
block/caching transport is a formatting change, not a re-architecture.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CHARTER_PATH = Path("config/charter.md")


def load_charter(path: Path | str = DEFAULT_CHARTER_PATH) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def _clock_block(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"<clock>\nCurrent time: {now.strftime('%Y-%m-%d %H:%M %Z')}\n</clock>"


def build_system_prompt(
    charter: str,
    *,
    context: str | None = None,
    now: datetime | None = None,
) -> str:
    """Assemble the per-turn system prompt from static + per-turn layers."""
    parts = [charter, _clock_block(now)]
    if context and context.strip():
        parts.append(
            "<context>\n"
            "Grounding retrieved for this turn. Prefer it over prior belief.\n\n"
            f"{context.strip()}\n"
            "</context>"
        )
    return "\n\n".join(parts)
