"""Doctor preflight: the pure decision logic (model diffing, token/exit/report
shaping) and that required_models reads the real models.yaml source of truth.

The live probes (HTTP/socket/launchctl) are thin I/O and exercised via
`sonar.sh doctor`; here we pin the logic that decides pass/warn/fail."""

from __future__ import annotations

import pytest

from sonar_harness import doctor
from sonar_harness.doctor import (
    Check,
    FAIL,
    OK,
    WARN,
    exit_code,
    format_report,
    missing_models,
    overall_status,
    required_models,
    token_status,
)


# ---- missing_models ----------------------------------------------------------
def test_missing_models_flags_absent_ids() -> None:
    installed = ["gemma4:e4b-mlx", "gemma4:12b-mlx"]
    assert missing_models(["gemma4:e4b-mlx"], installed) == []
    assert missing_models(["gemma4:26b-mlx"], installed) == ["gemma4:26b-mlx"]


def test_missing_models_bare_name_matches_any_tag() -> None:
    # obsidian_rag/ollama store the embedder as 'nomic-embed-text:latest'; a bare
    # required name must be satisfied by any installed tag of it.
    assert missing_models(["nomic-embed-text"], ["nomic-embed-text:latest"]) == []
    assert missing_models(["nomic-embed-text"], ["mxbai-embed-large:latest"]) == ["nomic-embed-text"]


# ---- token_status ------------------------------------------------------------
def test_token_status_not_connected_is_warn() -> None:
    assert token_status(None).status == WARN


def test_token_status_without_refresh_token_is_warn() -> None:
    assert token_status({"token": "abc"}).status == WARN


def test_token_status_with_refresh_token_is_ok() -> None:
    c = token_status({"refresh_token": "r", "scopes": ["a", "b"]})
    assert c.status == OK
    assert "2 scope" in c.detail


# ---- overall_status / exit_code ----------------------------------------------
def test_overall_status_precedence() -> None:
    assert overall_status([Check("a", OK), Check("b", WARN)]) == WARN
    assert overall_status([Check("a", WARN), Check("b", FAIL)]) == FAIL
    assert overall_status([Check("a", OK), Check("b", OK)]) == OK


def test_exit_code_nonzero_only_on_failure() -> None:
    assert exit_code([Check("a", OK), Check("b", WARN)]) == 0  # warnings don't fail
    assert exit_code([Check("a", FAIL)]) == 1


# ---- format_report -----------------------------------------------------------
def test_format_report_shows_fixes_only_for_non_ok() -> None:
    report = format_report([
        Check("Ollama", OK, "up", fix="should not appear"),
        Check("Models", FAIL, "missing x", fix="ollama pull x"),
    ])
    assert "should not appear" not in report
    assert "ollama pull x" in report
    assert "NOT fully ready" in report


def test_format_report_all_ok_summary() -> None:
    assert "ready" in format_report([Check("a", OK, "up")]).lower()


# ---- required_models reads the real models.yaml ------------------------------
def test_required_models_from_models_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SONAR_EMBED_MODEL", "nomic-embed-text")
    required, optional = required_models()
    # default (fast/e4b) + escalation (reason/12b) + embedder are required
    assert "gemma4:e4b-mlx" in required
    assert "gemma4:12b-mlx" in required
    assert "nomic-embed-text" in required
    # deep/26b is background-only -> optional, never double-counted as required
    assert "gemma4:26b-mlx" in optional
    assert not set(required) & set(optional)


def test_run_checks_returns_named_checks(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Smoke the wiring with every probe stubbed to a known-good system, so a
    # healthy stack yields zero failures deterministically (no real host reads).
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("SONAR_VAULT_PATH", str(tmp_path))  # a real, existing dir
    monkeypatch.setattr(doctor, "_ollama_installed_models",
                        lambda _b: ["gemma4:e4b-mlx", "gemma4:12b-mlx", "gemma4:26b-mlx", "nomic-embed-text:latest"])
    monkeypatch.setattr(doctor, "_http_json",
                        lambda url, timeout=2.0: {"status": "ok", "tools": [1, 2], "chunks": 10}
                        if "health" in url else {"results": []})
    monkeypatch.setattr(doctor, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(doctor, "_launchctl_loaded", lambda _l: True)
    monkeypatch.setattr(doctor, "_read_json", lambda _p: {"refresh_token": "r", "scopes": ["x"]})
    monkeypatch.setattr(doctor, "_formatter_config", lambda: tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text("x", encoding="utf-8")
    monkeypatch.setenv("SONAR_SEARCH_PROVIDER", "searxng")

    checks = doctor.run_checks()
    names = {c.name for c in checks}
    assert {"Ollama daemon", "Required models", "Vault", "Harness"} <= names
    assert exit_code(checks) == 0  # nothing failed on a healthy system
