"""Sonar preflight self-check — ``scripts/sonar.sh doctor``.

Verifies the whole local stack a fresh install (or a confused running one) needs
and prints one line per check — ok / warn / fail — with a fix hint. Exits
non-zero if any HARD dependency is missing, so it can gate an installer or a CI
smoke. This is the deeper companion to ``sonar.sh status`` (which just shows
ports + resident models): doctor knows what SHOULD be there and how to fix it.

The decision logic (which models are missing, token/exit-code/report shaping) is
pure and unit-tested; the probes below it are thin I/O.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"
_ICON = {OK: "✓", WARN: "!", FAIL: "✗"}

_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_MODELS_YAML = _HARNESS_ROOT / "config" / "models.yaml"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


# ---- pure decision logic (unit-tested) ---------------------------------------
def missing_models(required: list[str], installed: list[str]) -> list[str]:
    """Which ``required`` model ids Ollama does NOT have. Ollama reports tags as
    ``name:tag``; match the full id, and let a bare name (``nomic-embed-text``)
    satisfy any installed ``nomic-embed-text:<tag>``."""
    have = set(installed)
    have_bare = {m.split(":", 1)[0] for m in installed}
    return [
        m for m in required
        if m not in have and not (":" not in m and m in have_bare)
    ]


def token_status(token: dict | None) -> Check:
    """Google OAuth health from the stored credentials JSON. Missing or
    refresh-tokenless credentials are only a WARN — Sonar's read tools degrade
    gracefully to 'not connected' rather than crashing."""
    if token is None:
        return Check("Google OAuth", WARN, "not connected",
                     "connect Gmail/Calendar: scripts/sonar.sh google-auth")
    if not token.get("refresh_token"):
        return Check("Google OAuth", WARN, "credentials have no refresh_token (will expire)",
                     "re-run: scripts/sonar.sh google-auth")
    n = len(token.get("scopes") or [])
    return Check("Google OAuth", OK, f"connected ({n} scope(s)); keep the consent screen Published, "
                 "not Testing, or the refresh token expires in 7 days")


def overall_status(checks: list[Check]) -> str:
    if any(c.status == FAIL for c in checks):
        return FAIL
    if any(c.status == WARN for c in checks):
        return WARN
    return OK


def exit_code(checks: list[Check]) -> int:
    """Non-zero only on a hard failure; warnings don't fail the preflight."""
    return 1 if any(c.status == FAIL for c in checks) else 0


def format_report(checks: list[Check]) -> str:
    lines = ["Sonar doctor — preflight self-check", ""]
    width = max((len(c.name) for c in checks), default=0)
    for c in checks:
        lines.append(f"  [{_ICON[c.status]}] {c.name.ljust(width)}  {c.detail}")
        if c.fix and c.status != OK:
            lines.append(f"      -> {c.fix}")
    n_fail = sum(c.status == FAIL for c in checks)
    n_warn = sum(c.status == WARN for c in checks)
    lines.append("")
    if n_fail:
        lines.append(f"{n_fail} failed, {n_warn} warning(s) — Sonar is NOT fully ready.")
    elif n_warn:
        lines.append(f"All required checks passed, {n_warn} warning(s).")
    else:
        lines.append("All checks passed — Sonar is ready.")
    return "\n".join(lines)


def required_models() -> tuple[list[str], list[str]]:
    """(required, optional) model ids, from models.yaml + the embed env. Required:
    the default (voice/tool-select) + escalation (reasoning) models + the embedder
    the RAG index needs. Optional: the rest (e.g. `deep`/26b — background only)."""
    embed = os.environ.get("SONAR_EMBED_MODEL", "nomic-embed-text")
    try:
        from sonar_harness.model_router import load_models_config

        cfg = load_models_config(_MODELS_YAML)
        required = [cfg.resolve(cfg.default), cfg.resolve(cfg.escalation), embed]
        core = {cfg.default, cfg.escalation}
        optional = [v for k, v in cfg.aliases.items() if k not in core]
    except Exception:  # noqa: BLE001 — a broken models.yaml must not break the doctor
        required = ["gemma4:e4b-mlx", "gemma4:12b-mlx", embed]
        optional = ["gemma4:26b-mlx"]
    seen: set[str] = set()
    required = [m for m in required if not (m in seen or seen.add(m))]
    optional = [m for m in optional if m not in required]
    return required, optional


# ---- env resolution (mirrors server.py / sonar.sh) ---------------------------
def _ollama_url() -> str:
    return os.environ.get("SONAR_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


def _harness_url() -> str:
    base = os.environ.get("SONAR_HARNESS_URL")
    if base:
        return base.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('SONAR_PORT', '8787')}"


def _vault_path() -> Path:
    return Path(os.environ.get("SONAR_VAULT_PATH", str(Path.home() / "Documents" / "Obsidian Vault")))


def _glow_port() -> int:
    return int(os.environ.get("SONAR_GLOW_PORT", "8770"))


def _token_path() -> Path:
    # Mirror google_auth.py's resolution exactly (SONAR_GOOGLE_TOKEN overrides).
    env = os.environ.get("SONAR_GOOGLE_TOKEN")
    if env:
        return Path(env)
    base = os.environ.get("SONAR_CONFIG_DIR", str(Path.home() / ".config" / "sonar"))
    return Path(base) / "google_token.json"


def _formatter_config() -> Path:
    return Path(os.environ.get("SONAR_FORMATTER_CONFIG", "~/.obsidian-rag/config.yaml")).expanduser()


# ---- probes (thin I/O) -------------------------------------------------------
def _http_json(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 — localhost only
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — any failure means "not reachable"
        return None


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ollama_installed_models(base_url: str) -> list[str] | None:
    data = _http_json(base_url + "/api/tags")
    if not isinstance(data, dict):
        return None
    return [str(m.get("name", "")) for m in data.get("models", [])]


def _launchctl_loaded(label: str) -> bool:
    try:
        return subprocess.run(  # noqa: S603,S607 — fixed label, not user input
            ["launchctl", "list", label], capture_output=True, timeout=5
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/unreadable == not connected
        return None


def _dep_check(binary: str, *, required: bool, fix: str) -> Check:
    path = shutil.which(binary)
    if path:
        return Check(f"CLI: {binary}", OK, path)
    return Check(f"CLI: {binary}", FAIL if required else WARN, "not on PATH", fix)


def _search_check() -> Check:
    provider = os.environ.get("SONAR_SEARCH_PROVIDER", "").strip().lower()
    if not provider:
        return Check("Web search", WARN, "no provider set",
                     "set SONAR_SEARCH_PROVIDER=searxng (or tavily) in config/.env")
    if provider == "searxng":
        url = os.environ.get("SONAR_SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
        # Liveness via /config (JSON, instant) rather than /search — a real search
        # federates across engines (~3 s) and would both slow the doctor and fire a
        # live query on every run. /config confirms it's up AND serving JSON.
        if isinstance(_http_json(url + "/config", timeout=4.0), dict):
            return Check("Web search", OK, f"searxng up ({url})")
        return Check("Web search", WARN, f"searxng unreachable ({url})",
                     "start it: scripts/sonar.sh searxng up")
    if provider == "tavily":
        if os.environ.get("TAVILY_API_KEY"):
            return Check("Web search", OK, "tavily (key set)")
        return Check("Web search", WARN, "tavily selected but TAVILY_API_KEY unset",
                     "set TAVILY_API_KEY in config/.env")
    return Check("Web search", WARN, f"unknown provider {provider!r}", "use searxng or tavily")


def run_checks() -> list[Check]:
    """Probe the live system and return the ordered checks."""
    checks: list[Check] = []

    checks.append(_dep_check("uv", required=True, fix="install uv: https://docs.astral.sh/uv/"))
    checks.append(_dep_check("ollama", required=False, fix="install Ollama: https://ollama.com"))

    base = _ollama_url()
    installed = _ollama_installed_models(base)
    if installed is None:
        checks.append(Check("Ollama daemon", FAIL, f"unreachable at {base}",
                            "start Ollama (open -a Ollama) and enable it at login"))
    else:
        checks.append(Check("Ollama daemon", OK, f"up at {base} ({len(installed)} models)"))
        required, optional = required_models()
        miss = missing_models(required, installed)
        if miss:
            checks.append(Check("Required models", FAIL, "missing: " + ", ".join(miss),
                                "; ".join(f"ollama pull {m}" for m in miss)))
        else:
            checks.append(Check("Required models", OK, ", ".join(required)))
        opt_miss = missing_models(optional, installed)
        if opt_miss:
            checks.append(Check("Optional models", WARN, "missing: " + ", ".join(opt_miss),
                                "background/batch only; " + "; ".join(f"ollama pull {m}" for m in opt_miss)))

    vp = _vault_path()
    if vp.is_dir():
        checks.append(Check("Vault", OK, str(vp)))
    else:
        checks.append(Check("Vault", FAIL, f"not found: {vp}",
                            "set SONAR_VAULT_PATH to your Obsidian vault"))

    h = _http_json(_harness_url() + "/health")
    if not isinstance(h, dict) or h.get("status") != "ok":
        checks.append(Check("Harness", FAIL, f"not healthy at {_harness_url()}",
                            "start it: scripts/sonar.sh daemon install"))
    else:
        tools = len(h.get("tools", []))
        chunks = h.get("chunks", 0)
        if not chunks:
            checks.append(Check("Harness", WARN, f"up ({tools} tools) but vault index is empty",
                                "check the vault path + Ollama embeddings, then restart the harness"))
        else:
            checks.append(Check("Harness", OK, f"up — {tools} tools, {chunks} indexed chunks"))

    gp = _glow_port()
    if _port_open(gp):
        checks.append(Check("Overlay backend", OK, f":{gp} up (F5 box / voice)"))
    else:
        checks.append(Check("Overlay backend", WARN, f":{gp} down",
                            "typed box: daemon install  ·  voice: daemon install-voice"))

    for label in ("com.sonar.harness", "com.sonar.voice"):
        voice = label.endswith("voice")
        if _launchctl_loaded(label):
            checks.append(Check(f"Agent {label}", OK, "loaded (durable)"))
        else:
            checks.append(Check(f"Agent {label}", WARN, "not loaded" + (" (opt-in)" if voice else ""),
                                f"scripts/sonar.sh daemon {'install-voice' if voice else 'install'}"))

    checks.append(token_status(_read_json(_token_path())))
    checks.append(_search_check())

    fc = _formatter_config()
    if fc.exists():
        checks.append(Check("Formatter config", OK, str(fc)))
    else:
        checks.append(Check("Formatter config", WARN, f"missing: {fc}",
                            "auto-generated on next harness start (scripts/sonar.sh daemon install)"))

    return checks


def main() -> int:
    checks = run_checks()
    print(format_report(checks))
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
