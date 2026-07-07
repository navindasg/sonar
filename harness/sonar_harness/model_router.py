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
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("sonar.router")

DEFAULT_MODELS_CONFIG_PATH = Path("config/models.yaml")


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

    def resolve(self, model_or_alias: str) -> str:
        """Return the full Ollama model id for an alias or a pass-through id."""
        if model_or_alias in self.aliases:
            return self.aliases[model_or_alias]
        if model_or_alias in self.aliases.values():
            return model_or_alias
        raise UnknownModel(
            f"unknown model {model_or_alias!r}; known aliases: {sorted(self.aliases)}"
        )


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
    return ModelsConfig(
        default=str(default),
        escalation=str(escalation),
        aliases={str(k): str(v) for k, v in aliases.items()},
        escalate_synthesis_after_tools=bool(escalate_synth),
    )


def wants_escalation(user_text: str, config: ModelsConfig) -> bool:
    """STUB: the future difficulty classifier. Always False for now.

    Kept as a named seam so the agent loop's escalation branch is exercised
    and testable; a later pass swaps the body for a real classifier.
    """
    del user_text, config
    return False


def pick_model(user_text: str, config: ModelsConfig | None = None) -> tuple[str, bool]:
    """Return ``(full_model_id, escalated)`` for this turn.

    Precedence: escalation classifier -> global default. ``escalated`` lets the
    caller emit a ``model_switch`` step-event when the router flips models.
    """
    cfg = config if config is not None else load_config()
    if wants_escalation(user_text, cfg):
        return cfg.resolve(cfg.escalation), True
    return cfg.resolve(cfg.default), False
