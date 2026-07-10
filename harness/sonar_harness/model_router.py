"""Per-turn model selection for the agent's tool-loop.

Ported from brook37 ``daemon/model_router.py`` and reduced to Sonar's
precedence chain (see ``config/models.yaml``):

  1. per-turn escalation classifier  -> STUBBED (``wants_escalation`` returns
     False; the hook exists so a future classifier flips a hard turn to the
     26b reasoning model without touching the agent loop).
  2. global default (``fast`` = gemma4:e4b-mlx).

Aliases resolve to the full Ollama model id. An unknown alias raises rather
than being silently passed to Ollama, which would 404 mid-turn.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger("sonar.router")

DEFAULT_MODELS_CONFIG_PATH = Path("config/models.yaml")

# keep_alive when a model has none configured: -1 pins it resident forever. The
# router only overrides this for models the config explicitly wants transient
# (e.g. the 26b reasoner: loaded on demand, unloaded when idle so it frees RAM).
_DEFAULT_KEEP_ALIVE: str | int = -1


class UnknownModel(ValueError):
    """Raised when a model alias or id is not recognized."""


@dataclass(frozen=True)
class ModelsConfig:
    default: str
    escalation: str
    aliases: dict[str, str]
    # When a turn used tools, redo the final grounded answer on `escalation`
    # (see sonar_harness/agent.py). The fast model selects tools well but
    # synthesizes weakly. Toggle via config/models.yaml.
    escalate_synthesis_after_tools: bool = True
    # Per-turn difficulty router (see `wants_escalation`): a turn that looks like
    # real reasoning/drafting runs on `escalation`; chit-chat + tool lookups stay
    # on `default`. Config-driven so it tunes without a code change.
    difficulty_enabled: bool = False
    difficulty_min_words: int = 0
    difficulty_triggers: tuple[str, ...] = ()
    # Per-alias Ollama keep_alive (seconds int, or a Go duration string like
    # "8m"). Absent aliases fall back to `_DEFAULT_KEEP_ALIVE` (pinned).
    keep_alive: dict[str, str | int] = field(default_factory=dict)

    def resolve(self, model_or_alias: str) -> str:
        """Return the full Ollama model id for an alias or a pass-through id."""
        if model_or_alias in self.aliases:
            return self.aliases[model_or_alias]
        if model_or_alias in self.aliases.values():
            return model_or_alias
        raise UnknownModel(
            f"unknown model {model_or_alias!r}; known aliases: {sorted(self.aliases)}"
        )

    def keep_alive_for(self, model_id_or_alias: str) -> str | int:
        """Return the configured keep_alive for a model (by alias or full id).

        Lets one turn pin the always-on fast model while the on-demand reasoner
        uses a short idle TTL, so the 26b only occupies RAM while in active use.
        Defaults to `_DEFAULT_KEEP_ALIVE` (pinned) when unconfigured.
        """
        if model_id_or_alias in self.keep_alive:
            return self.keep_alive[model_id_or_alias]
        for alias, mid in self.aliases.items():
            if mid == model_id_or_alias and alias in self.keep_alive:
                return self.keep_alive[alias]
        return _DEFAULT_KEEP_ALIVE


def load_config(path: Path | str = DEFAULT_MODELS_CONFIG_PATH) -> ModelsConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    default = raw.get("default")
    if not isinstance(default, str) or not default:
        raise RuntimeError(f"{path}: missing or empty 'default' model")
    aliases = raw.get("aliases") or {}
    if not isinstance(aliases, dict) or not aliases:
        raise RuntimeError(f"{path}: 'aliases' must be a non-empty mapping")
    escalation = raw.get("escalation") or default
    escalate_synth = raw.get("escalate_synthesis_after_tools", True)

    diff = raw.get("difficulty") if isinstance(raw.get("difficulty"), dict) else {}
    triggers = diff.get("triggers") or []
    if not isinstance(triggers, list):
        raise RuntimeError(f"{path}: 'difficulty.triggers' must be a list")

    keep_alive_raw = raw.get("keep_alive") or {}
    if not isinstance(keep_alive_raw, dict):
        raise RuntimeError(f"{path}: 'keep_alive' must be a mapping")

    return ModelsConfig(
        default=str(default),
        escalation=str(escalation),
        aliases={str(k): str(v) for k, v in aliases.items()},
        escalate_synthesis_after_tools=bool(escalate_synth),
        difficulty_enabled=bool(diff.get("enabled", False)),
        difficulty_min_words=int(diff.get("min_words", 0) or 0),
        difficulty_triggers=tuple(str(t) for t in triggers),
        keep_alive={str(k): v for k, v in keep_alive_raw.items()},
    )


@lru_cache(maxsize=8)
def _trigger_pattern(triggers: tuple[str, ...]) -> re.Pattern[str] | None:
    """Compile the trigger fragments into one word-boundary alternation (cached).

    Each config fragment is a small regex (e.g. ``analy[sz]e``, ``should i``);
    an unparseable fragment is dropped with a warning rather than crashing the
    turn — a bad pattern must not take the whole assistant down.
    """
    if not triggers:
        return None
    try:
        return re.compile(r"\b(?:" + "|".join(triggers) + r")\b", re.IGNORECASE)
    except re.error as exc:
        log.warning("bad difficulty trigger pattern %r: %s", triggers, exc)
        return None


def wants_escalation(user_text: str, config: ModelsConfig) -> bool:
    """True when this turn should run on the reasoning model.

    A cheap, zero-latency difficulty classifier (no extra model call): the turn
    escalates if it is long enough to be involved (``difficulty_min_words``) or
    its wording matches a reasoning/drafting trigger. Disabled turns and every
    short chit-chat/tool-lookup stay on the fast model.
    """
    if not config.difficulty_enabled:
        return False
    text = (user_text or "").strip()
    if not text:
        return False
    if config.difficulty_min_words > 0 and len(text.split()) >= config.difficulty_min_words:
        return True
    pattern = _trigger_pattern(config.difficulty_triggers)
    return bool(pattern and pattern.search(text))


def pick_model(user_text: str, config: ModelsConfig | None = None) -> tuple[str, bool]:
    """Return ``(full_model_id, escalated)`` for this turn.

    Precedence: escalation classifier -> global default. ``escalated`` lets the
    caller emit a ``model_switch`` step-event when the router flips models.
    """
    cfg = config if config is not None else load_config()
    if wants_escalation(user_text, cfg):
        return cfg.resolve(cfg.escalation), True
    return cfg.resolve(cfg.default), False
