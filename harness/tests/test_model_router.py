"""Unit tests for the per-turn model router: difficulty classifier + keep_alive."""
from __future__ import annotations

import pytest

from sonar_harness.model_router import (
    ModelsConfig,
    UnknownModel,
    load_config,
    pick_model,
    wants_escalation,
)

FAST = "gemma4:e4b-mlx"
REASON = "gemma4:26b-mlx"

_TRIGGERS = ("analy[sz]e", "compare", r"\bwhy\b", "should i\\b", "draft")


def _cfg(*, enabled: bool = True, min_words: int = 40) -> ModelsConfig:
    return ModelsConfig(
        default="fast",
        escalation="reason",
        aliases={"fast": FAST, "reason": REASON},
        difficulty_enabled=enabled,
        difficulty_min_words=min_words,
        difficulty_triggers=_TRIGGERS,
        keep_alive={"fast": -1, "reason": "8m"},
    )


# ---- resolve / keep_alive_for -------------------------------------------------

def test_resolve_alias_id_and_unknown() -> None:
    cfg = _cfg()
    assert cfg.resolve("fast") == FAST
    assert cfg.resolve(REASON) == REASON  # pass-through of a known id
    with pytest.raises(UnknownModel):
        cfg.resolve("nope")


def test_keep_alive_for_by_alias_and_id() -> None:
    cfg = _cfg()
    assert cfg.keep_alive_for("fast") == -1
    assert cfg.keep_alive_for(FAST) == -1        # resolved id maps via its alias
    assert cfg.keep_alive_for("reason") == "8m"
    assert cfg.keep_alive_for(REASON) == "8m"


def test_keep_alive_defaults_to_pinned_when_unconfigured() -> None:
    cfg = ModelsConfig(default="fast", escalation="reason", aliases={"fast": FAST})
    assert cfg.keep_alive_for("fast") == -1
    assert cfg.keep_alive_for("anything") == -1


# ---- wants_escalation ---------------------------------------------------------

def test_disabled_never_escalates() -> None:
    cfg = _cfg(enabled=False)
    assert wants_escalation("please analyze the tradeoffs in depth", cfg) is False
    assert wants_escalation("x " * 100, cfg) is False


def test_trigger_word_escalates() -> None:
    cfg = _cfg()
    assert wants_escalation("compare Caddy vs nginx for me", cfg) is True
    assert wants_escalation("why did the deploy fail?", cfg) is True
    assert wants_escalation("draft a reply to Sam", cfg) is True


def test_short_chitchat_and_tool_lookups_stay_fast() -> None:
    cfg = _cfg()
    assert wants_escalation("hey what's up", cfg) is False
    assert wants_escalation("what are my todos for today", cfg) is False
    assert wants_escalation("", cfg) is False


def test_long_involved_turn_escalates_on_length() -> None:
    cfg = _cfg(min_words=40)
    long_turn = "i need help thinking through " + "and ".join(["this"] * 40)
    assert len(long_turn.split()) >= 40
    assert wants_escalation(long_turn, cfg) is True


def test_min_words_zero_disables_length_rule() -> None:
    cfg = _cfg(min_words=0)
    # No trigger word, long input -> stays fast because length rule is off.
    assert wants_escalation("blah " * 80, cfg) is False


def test_trigger_matching_is_word_bounded() -> None:
    cfg = _cfg()
    # "whyever" must not match the \bwhy\b trigger.
    assert wants_escalation("whyever would that be", cfg) is False


# ---- pick_model ---------------------------------------------------------------

def test_pick_model_escalates_hard_turn() -> None:
    model, escalated = pick_model("analyze this for me", _cfg())
    assert model == REASON and escalated is True


def test_pick_model_keeps_easy_turn_fast() -> None:
    model, escalated = pick_model("hi there", _cfg())
    assert model == FAST and escalated is False


# ---- load_config --------------------------------------------------------------

_YAML = """
default: fast
escalation: reason
difficulty:
  enabled: true
  min_words: 30
  triggers:
    - 'compare'
    - 'draft'
keep_alive:
  fast: -1
  reason: "8m"
aliases:
  fast: "gemma4:e4b-mlx"
  reason: "gemma4:26b-mlx"
"""


def test_load_config_parses_difficulty_and_keep_alive(tmp_path) -> None:
    p = tmp_path / "models.yaml"
    p.write_text(_YAML)
    cfg = load_config(p)
    assert cfg.difficulty_enabled is True
    assert cfg.difficulty_min_words == 30
    assert "compare" in cfg.difficulty_triggers
    assert cfg.keep_alive_for("reason") == "8m"
    assert wants_escalation("compare a and b", cfg) is True


def test_load_config_rejects_bad_shapes(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("default: fast\naliases:\n  fast: x\nkeep_alive: 5\n")
    with pytest.raises(RuntimeError):
        load_config(p)
