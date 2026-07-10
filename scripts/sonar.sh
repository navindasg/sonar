#!/usr/bin/env bash
#
# sonar.sh — start/stop the Sonar harness + overlay bridge as detached daemons.
#
# Why this exists: launching these from an editor/agent session ties them to
# that session, so they die when it cleans up its child processes. This starts
# them with nohup, orphaned into launchd, so they keep running on their own
# until you stop them. Logs and pidfiles live under $SONAR_HOME (default
# ~/.sonar); nothing is written into the repo or your vault.
#
#   scripts/sonar.sh up            # start harness (:8787) + bridge (:8770)
#   scripts/sonar.sh down          # stop both
#   scripts/sonar.sh restart       # down, then up
#   scripts/sonar.sh status        # ports, /health, models Ollama has resident
#   scripts/sonar.sh logs          # tail -f both logs (Ctrl-C to stop tailing)
#   scripts/sonar.sh ask "..."     # one-shot question against the running harness
#   scripts/sonar.sh voice         # full voice loop (mic->STT->harness->TTS), foreground
#   scripts/sonar.sh google-auth   # one-time browser consent for Gmail + Calendar (read-only)
#
# Config (all optional, override via env):
#   SONAR_HOME        state dir for logs/pids   (default ~/.sonar)
#   SONAR_PORT        harness port              (default 8787)
#   SONAR_GLOW_PORT   overlay bridge port       (default 8770)
#   SONAR_VAULT_PATH  Obsidian vault to index   (default ~/Documents/Obsidian Vault)
#   SONAR_VAULT_NAME  index name                (default sonar)
#   SONAR_OLLAMA_URL  Ollama base url           (default http://127.0.0.1:11434)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load config/.env early (gitignored: web-search provider/key, Google token paths,
# any SONAR_* overrides) so its values feed both the ${VAR:-default} config below
# AND every process we spawn — the harness reads straight from os.environ. `set -a`
# exports each assignment so `uv run` children inherit it.
if [ -f "$REPO_ROOT/config/.env" ]; then
  set -a; . "$REPO_ROOT/config/.env"; set +a
fi

# --- config (override via env) ----------------------------------------------
SONAR_HOME="${SONAR_HOME:-$HOME/.sonar}"
HARNESS_PORT="${SONAR_PORT:-8787}"
GLOW_PORT="${SONAR_GLOW_PORT:-8770}"
VAULT_PATH="${SONAR_VAULT_PATH:-$HOME/Documents/Obsidian Vault}"
VAULT_NAME="${SONAR_VAULT_NAME:-sonar}"
OLLAMA_URL="${SONAR_OLLAMA_URL:-http://127.0.0.1:11434}"

LOG_DIR="$SONAR_HOME/logs"
RUN_DIR="$SONAR_HOME/run"

# --- helpers ----------------------------------------------------------------
_port_up()  { lsof -ti "tcp:$1" >/dev/null 2>&1; }
_health()   { curl -sf "http://127.0.0.1:${HARNESS_PORT}/health" 2>/dev/null; }

# Spawn a command fully detached from THIS shell: the subshell backgrounds it
# and exits immediately, so nohup's child is orphaned (reparented to launchd)
# and survives us. Its PID is recorded for status/down.
_spawn() {
  local pidfile="$1" logfile="$2"; shift 2
  ( cd "$REPO_ROOT" && nohup "$@" >"$logfile" 2>&1 & echo $! >"$pidfile" )
}

_hs_installed() { [ -d "/Applications/Hammerspoon.app" ] || [ -d "$HOME/Applications/Hammerspoon.app" ]; }
_hs_running()   { pgrep -xq Hammerspoon; }
_remap_active() { hidutil property --get "UserKeyMapping" 2>/dev/null | grep -q "51539607759"; }

# The overlay is a Hammerspoon config (~/.hammerspoon/init.lua -> glow). Start HS
# if installed and not already up; the overlay stays HIDDEN until you press F5.
# Non-fatal if HS is absent — the harness + bridge still run (use `ask`).
_ensure_hammerspoon() {
  if ! _hs_installed; then
    echo "overlay: Hammerspoon not installed — skipping (harness + bridge still up)"
    return 0
  fi
  if _hs_running; then
    echo "overlay: Hammerspoon already running"
  else
    echo "overlay: starting Hammerspoon ..."
    open -a Hammerspoon
  fi
  # F5 only reaches the overlay if the (volatile) F5->F13 remap is applied.
  _remap_active || echo "overlay: ! F5->F13 remap not active — run scripts/hotkey/remap.sh (or use the ⌘⌃⌥G chord)"
}

_wait_for() {  # _wait_for <label> <predicate-cmd...> — poll ~120s
  local label="$1"; shift
  local i
  for i in $(seq 1 120); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "  ! timed out waiting for ${label}" >&2
  return 1
}

# Ollama + vault must be present before we start anything model-backed.
_preflight() {
  if ! curl -sf "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
    echo "error: Ollama not reachable at ${OLLAMA_URL} — start it with 'ollama serve'." >&2
    exit 1
  fi
  if [ ! -d "$VAULT_PATH" ]; then
    echo "error: vault not found at '${VAULT_PATH}' (set SONAR_VAULT_PATH)." >&2
    exit 1
  fi
}

# Start the harness daemon on :$HARNESS_PORT if it isn't already up.
_start_harness() {
  if _port_up "$HARNESS_PORT"; then
    echo "harness already up on :${HARNESS_PORT}"
    return 0
  fi
  echo "starting harness on :${HARNESS_PORT} (vault: ${VAULT_PATH}) ..."
  _spawn "$RUN_DIR/harness.pid" "$LOG_DIR/harness.log" \
    env SONAR_PORT="$HARNESS_PORT" \
        SONAR_VAULT_PATH="$VAULT_PATH" \
        SONAR_VAULT_NAME="$VAULT_NAME" \
        SONAR_OLLAMA_URL="$OLLAMA_URL" \
        PYTHONUNBUFFERED=1 \
        uv run --project harness python -u -m sonar_harness
  _wait_for "harness /health" _health \
    || { echo "  see $LOG_DIR/harness.log"; tail -n 15 "$LOG_DIR/harness.log" >&2; exit 1; }
  echo "  harness ready."
}

# --- subcommands ------------------------------------------------------------
cmd_up() {
  mkdir -p "$LOG_DIR" "$RUN_DIR"
  _preflight
  _start_harness

  # Bridge -------------------------------------------------------------------
  if _port_up "$GLOW_PORT"; then
    echo "bridge already up on :${GLOW_PORT}"
  else
    echo "starting overlay bridge on :${GLOW_PORT} ..."
    _spawn "$RUN_DIR/bridge.pid" "$LOG_DIR/bridge.log" \
      env SONAR_HARNESS_URL="http://127.0.0.1:${HARNESS_PORT}" \
          SONAR_GLOW_PORT="$GLOW_PORT" \
          PYTHONUNBUFFERED=1 \
          uv run overlay/bridge.py
    _wait_for "bridge :${GLOW_PORT}" _port_up "$GLOW_PORT" \
      || { echo "  see $LOG_DIR/bridge.log"; tail -n 15 "$LOG_DIR/bridge.log" >&2; exit 1; }
    echo "  bridge ready."
  fi

  # Overlay ------------------------------------------------------------------
  _ensure_hammerspoon

  echo
  cmd_status
  echo
  echo "Test it:   scripts/sonar.sh ask \"According to my notes, what reverse proxy does AIAM use?\""
  echo "Overlay:   press F5, type a question, Enter (expand the panel for steps)."
  echo "Voice:     scripts/sonar.sh voice   (mic->STT->harness->TTS; takes over :${GLOW_PORT})"
  echo "Logs:      scripts/sonar.sh logs"
}

# Full voice loop, run in the FOREGROUND: it needs mic + speaker, whose macOS
# permissions bind to the launching terminal, and you'll want to watch the
# STT/TTS logs live. It serves the overlay on :$GLOW_PORT, so it takes that port
# over from the typed bridge (stopped here if running). Ctrl-C returns you to the
# typed bridge via `scripts/sonar.sh up`.
cmd_voice() {
  mkdir -p "$LOG_DIR" "$RUN_DIR"
  _preflight
  _start_harness

  # The typed bridge and the voice loop both serve the overlay on :$GLOW_PORT;
  # only one can hold it. Hand the port to the voice loop.
  if _port_up "$GLOW_PORT"; then
    echo "freeing :${GLOW_PORT} (stopping typed bridge for the voice loop) ..."
    lsof -ti "tcp:$GLOW_PORT" 2>/dev/null | xargs kill 2>/dev/null || true
    rm -f "$RUN_DIR/bridge.pid"
    _wait_for ":${GLOW_PORT} free" bash -c "! lsof -ti tcp:${GLOW_PORT} >/dev/null 2>&1" || true
  fi

  _ensure_hammerspoon
  echo
  echo "starting voice loop on :${GLOW_PORT} — press F5, speak, listen. Ctrl-C to stop."
  echo "(first run downloads STT + TTS models and prompts for Microphone access)"
  echo
  # Foreground exec: inherits this terminal's TTY + mic/speaker TCC grants.
  cd "$REPO_ROOT/voice" && exec env \
    SONAR_HARNESS_URL="http://127.0.0.1:${HARNESS_PORT}" \
    SONAR_GLOW_PORT="$GLOW_PORT" \
    PYTHONUNBUFFERED=1 \
    uv run voice_loop.py
}

# One-time Google consent for Gmail + Calendar (read-only, per-user OAuth — no
# admin needed). Foreground: it opens a browser for you to approve, then saves a
# self-refreshing token under ~/.config/sonar. See harness/sonar_harness/
# google_auth.py for the zero-admin Google Cloud Console setup steps.
cmd_google_auth() {
  echo "Opening Google consent (one-time). A browser window will open — approve read-only access."
  cd "$REPO_ROOT" && exec uv run --project harness python -m sonar_harness.google_auth
}

cmd_down() {
  local p pids
  for p in "$HARNESS_PORT" "$GLOW_PORT"; do
    pids="$(lsof -ti "tcp:$p" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
      echo "$pids" | xargs kill 2>/dev/null || true
      echo "stopped :$p"
    else
      echo ":$p not running"
    fi
  done
  rm -f "$RUN_DIR/harness.pid" "$RUN_DIR/bridge.pid"
}

cmd_status() {
  if _port_up "$HARNESS_PORT"; then echo "harness  :${HARNESS_PORT}  UP"; else echo "harness  :${HARNESS_PORT}  down"; fi
  if _port_up "$GLOW_PORT";    then echo "bridge   :${GLOW_PORT}  UP"; else echo "bridge   :${GLOW_PORT}  down"; fi
  if _hs_running; then echo "overlay  Hammerspoon  UP$(_remap_active || echo '  (F5 remap INACTIVE — chord ⌘⌃⌥G only)')"; else echo "overlay  Hammerspoon  down"; fi
  local h; h="$(_health || true)"
  if [ -n "$h" ]; then
    echo "$h" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"  tools: {len(d[\"tools\"])}  chunks: {d[\"chunks\"]}  model: {d[\"default_model\"]}")' 2>/dev/null || true
  fi
  if command -v ollama >/dev/null 2>&1; then
    echo "  resident models:"; ollama ps 2>/dev/null | sed 's/^/    /'
  fi
}

cmd_logs() {
  mkdir -p "$LOG_DIR"
  : >>"$LOG_DIR/harness.log"; : >>"$LOG_DIR/bridge.log"
  tail -n 20 -f "$LOG_DIR/harness.log" "$LOG_DIR/bridge.log"
}

cmd_ask() {
  local q="$*"
  [ -n "$q" ] || { echo "usage: sonar.sh ask <question>" >&2; exit 1; }
  _port_up "$HARNESS_PORT" || { echo "harness not running — 'sonar.sh up' first." >&2; exit 1; }
  python3 - "$q" "$HARNESS_PORT" <<'PY'
import json, sys, time, urllib.request
q, port = sys.argv[1], sys.argv[2]
body = json.dumps({"stream": False, "messages": [{"role": "user", "content": q}]}).encode()
req = urllib.request.Request(
    f"http://127.0.0.1:{port}/v1/chat/completions",
    data=body, headers={"content-type": "application/json"})
t = time.time()
r = json.load(urllib.request.urlopen(req, timeout=180))
dt = time.time() - t
xs = r.get("x_sonar", {})
print(r["choices"][0]["message"]["content"])
print(f"\n[{dt:.1f}s · model={r.get('model')} · tool_calls={xs.get('tool_calls', 0)}]")
PY
}

# Self-hosted SearXNG for the web.search tool (private metasearch, no vendor key).
# Thin lifecycle wrapper over infra/searxng/{up.sh,docker-compose.yml}.
cmd_searxng() {
  local action="${1:-up}"
  local dir="$REPO_ROOT/infra/searxng"
  case "$action" in
    up)     "$dir/up.sh" ;;
    down)   ( cd "$dir" && docker compose down ) ;;
    logs)   ( cd "$dir" && docker compose logs -f --tail=40 ) ;;
    status) ( cd "$dir" && docker compose ps ) ;;
    *) echo "usage: sonar.sh searxng [up|down|logs|status]" >&2; exit 1 ;;
  esac
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    up)      cmd_up ;;
    down)    cmd_down ;;
    restart) cmd_down; echo; cmd_up ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    ask)         cmd_ask "$@" ;;
    voice)       cmd_voice ;;
    google-auth) cmd_google_auth ;;
    searxng)     cmd_searxng "$@" ;;
    ""|-h|--help|help)
      sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
    *)
      echo "unknown command: $sub (try: up | down | restart | status | logs | ask | voice | google-auth | searxng)" >&2
      exit 1 ;;
  esac
}

main "$@"
