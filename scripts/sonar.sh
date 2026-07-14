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
#   scripts/sonar.sh daemon <cmd>  # durable launchd agents: install | install-voice | uninstall | status
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

# launchd starts LaunchAgents with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin)
# that omits ~/.local/bin (uv) and Homebrew, and it does NOT source your shell
# profile. Prepend the usual locations so `uv`/`ollama` resolve identically
# whether we're run from a login shell, an editor, or a launchd agent — this also
# un-breaks the 08:00 morning-brief agent, which shells out to `uv` the same way.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

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

# Absolute path to THIS script, for launchd plists. Must NOT be ${BASH_SOURCE[0]}
# — that echoes however we were invoked (e.g. the relative "scripts/sonar.sh"),
# and launchd runs agents from "/", where a relative path fails to resolve.
SONAR_SH="$REPO_ROOT/scripts/sonar.sh"

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

# Block until Ollama answers, or give up after ~90s so launchd's KeepAlive can
# retry cleanly. Used by the launchd exec paths: the harness indexes the vault
# through Ollama embeddings at startup and aborts if Ollama is down, so at login
# we wait for it (Ollama's own app may still be coming up) instead of crash-looping.
_await_ollama() {
  local i
  for i in $(seq 1 90); do
    curl -sf "${OLLAMA_URL}/api/tags" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "error: Ollama not reachable at ${OLLAMA_URL} after 90s — retrying." >&2
  return 1
}

# launchd-agent lifecycle helpers (used by cmd_daemon). Templates live under
# scripts/launchd/; __SONAR_SH__ must be the ABSOLUTE $SONAR_SH (launchd runs
# agents from "/"), __LOGDIR__ the log dir.
_install_agent() {  # <template-src> <installed-dst>
  local la; la="$(dirname "$2")"; mkdir -p "$la"
  sed -e "s|__SONAR_SH__|${SONAR_SH}|g" -e "s|__LOGDIR__|${LOG_DIR}|g" "$1" > "$2"
  launchctl unload "$2" 2>/dev/null || true
  launchctl load "$2"
}
_remove_agent() {  # <installed-dst>
  [ -e "$1" ] || return 0
  launchctl unload "$1" 2>/dev/null || true
  rm -f "$1"
}
# Kill a manually-started (`sonar.sh up`) process still holding a port, so an
# agent can bind it. <label> <port> <pidfile-name>
_free_manual() {
  if _port_up "$2"; then
    echo "stopping manual $1 on :$2 (agent will take it) ..."
    lsof -ti "tcp:$2" 2>/dev/null | xargs kill 2>/dev/null || true
    rm -f "$RUN_DIR/$3"; sleep 1
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

# Morning brief: fetch daily.brief from the harness, save a vault note, speak it
# (if the voice loop is up). `run` does it once; `install`/`uninstall` manage the
# 08:00 launchd schedule.
cmd_brief() {
  local action="${1:-run}"
  local plist_src="$REPO_ROOT/scripts/launchd/com.sonar.morning-brief.plist"
  local plist_dst="$HOME/Library/LaunchAgents/com.sonar.morning-brief.plist"
  case "$action" in
    run|"")
      _port_up "$HARNESS_PORT" || { echo "harness not running — 'sonar.sh up' first." >&2; exit 1; }
      cd "$REPO_ROOT" && exec env \
        SONAR_HARNESS_URL="http://127.0.0.1:${HARNESS_PORT}" \
        SONAR_GLOW_PORT="$GLOW_PORT" \
        SONAR_VAULT_PATH="$VAULT_PATH" \
        PYTHONUNBUFFERED=1 \
        uv run scripts/morning_brief.py
      ;;
    install)
      mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
      sed -e "s|__SONAR_SH__|${SONAR_SH}|g" -e "s|__LOGDIR__|${LOG_DIR}|g" \
        "$plist_src" > "$plist_dst"
      launchctl unload "$plist_dst" 2>/dev/null || true
      launchctl load "$plist_dst"
      echo "morning brief scheduled daily at 08:00 (com.sonar.morning-brief)."
      echo "  plist: $plist_dst"
      echo "  note: keep the harness (+ voice loop for audio) running at 08:00."
      ;;
    uninstall)
      launchctl unload "$plist_dst" 2>/dev/null || true
      rm -f "$plist_dst"
      echo "morning brief schedule removed."
      ;;
    *) echo "usage: sonar.sh brief [run|install|uninstall]" >&2; exit 1 ;;
  esac
}

# launchd entrypoint (hidden): run the harness in the FOREGROUND so launchd is its
# parent and KeepAlive can supervise it. No _spawn/nohup here — detaching would
# defeat launchd's supervision. Waits for Ollama first (see _await_ollama).
cmd_exec_harness() {
  mkdir -p "$LOG_DIR" "$RUN_DIR"
  [ -d "$VAULT_PATH" ] || { echo "error: vault not found at '${VAULT_PATH}'." >&2; exit 1; }
  _await_ollama || exit 1
  cd "$REPO_ROOT" && exec env \
    SONAR_PORT="$HARNESS_PORT" \
    SONAR_VAULT_PATH="$VAULT_PATH" \
    SONAR_VAULT_NAME="$VAULT_NAME" \
    SONAR_OLLAMA_URL="$OLLAMA_URL" \
    PYTHONUNBUFFERED=1 \
    uv run --project harness python -u -m sonar_harness
}

# launchd entrypoint (hidden) for the overlay bridge (WS :$GLOW_PORT; the
# Hammerspoon glow is the client and reconnects on its own). Waits for the
# harness it proxies to, then execs the bridge in the foreground so launchd's
# KeepAlive can supervise it. Mutually exclusive with the voice loop on :$GLOW_PORT.
cmd_exec_bridge() {
  mkdir -p "$LOG_DIR" "$RUN_DIR"
  _wait_for "harness /health" _health || exit 1
  cd "$REPO_ROOT" && exec env \
    SONAR_HARNESS_URL="http://127.0.0.1:${HARNESS_PORT}" \
    SONAR_GLOW_PORT="$GLOW_PORT" \
    PYTHONUNBUFFERED=1 \
    uv run overlay/bridge.py
}

# launchd entrypoint (hidden) for the voice loop. Waits for Ollama + the harness,
# then frees :$GLOW_PORT from the typed bridge (only one can hold it) and execs
# the loop in the foreground. Mic/TCC under launchd is EXPERIMENTAL — see the
# plist comment and `daemon install-voice`.
cmd_exec_voice() {
  mkdir -p "$LOG_DIR" "$RUN_DIR"
  _await_ollama || exit 1
  _wait_for "harness /health" _health || exit 1
  if _port_up "$GLOW_PORT"; then
    lsof -ti "tcp:$GLOW_PORT" 2>/dev/null | xargs kill 2>/dev/null || true
    rm -f "$RUN_DIR/bridge.pid"
    _wait_for ":${GLOW_PORT} free" bash -c "! lsof -ti tcp:${GLOW_PORT} >/dev/null 2>&1" || true
  fi
  cd "$REPO_ROOT/voice" && exec env \
    SONAR_HARNESS_URL="http://127.0.0.1:${HARNESS_PORT}" \
    SONAR_GLOW_PORT="$GLOW_PORT" \
    PYTHONUNBUFFERED=1 \
    uv run voice_loop.py
}

# Durable launchd LaunchAgents (RunAtLoad + KeepAlive): the harness — and, opt-in,
# the voice loop — start at login and respawn on crash. This is the fix for
# "the services died between sessions, so the 08:00 brief couldn't fire", and the
# first real step toward the packaged, always-on app.
cmd_daemon() {
  local action="${1:-status}"
  local la="$HOME/Library/LaunchAgents"
  local hsrc="$REPO_ROOT/scripts/launchd/com.sonar.harness.plist"
  local bsrc="$REPO_ROOT/scripts/launchd/com.sonar.bridge.plist"
  local vsrc="$REPO_ROOT/scripts/launchd/com.sonar.voice.plist"
  local hdst="$la/com.sonar.harness.plist"
  local bdst="$la/com.sonar.bridge.plist"
  local vdst="$la/com.sonar.voice.plist"
  case "$action" in
    install)
      # The durable typed-overlay stack: harness (+ RAG/tools) and the bridge that
      # backs the F5 box. Bridge and voice both bind :$GLOW_PORT, so drop any voice
      # agent first — they're mutually exclusive.
      mkdir -p "$LOG_DIR" "$RUN_DIR" "$la"
      if [ -e "$vdst" ]; then echo "removing voice agent (bridge owns :${GLOW_PORT}) ..."; _remove_agent "$vdst"; fi
      _free_manual harness "$HARNESS_PORT" harness.pid
      _free_manual bridge  "$GLOW_PORT"    bridge.pid
      _install_agent "$hsrc" "$hdst"
      echo "harness daemon installed (com.sonar.harness) — RunAtLoad + KeepAlive."
      if _wait_for "harness /health" _health; then echo "  harness up on :${HARNESS_PORT}."; else echo "  ! not healthy yet — check $LOG_DIR/harness.err.log"; fi
      _install_agent "$bsrc" "$bdst"
      echo "bridge daemon installed (com.sonar.bridge) — RunAtLoad + KeepAlive."
      if _wait_for "bridge :${GLOW_PORT}" _port_up "$GLOW_PORT"; then echo "  bridge up on :${GLOW_PORT} — F5 overlay should work."; else echo "  ! bridge not up — check $LOG_DIR/bridge.err.log"; fi
      _ensure_hammerspoon
      echo
      echo "Voice loop is opt-in (swaps the bridge off :${GLOW_PORT}; mic/TCC under launchd needs a live check):"
      echo "  scripts/sonar.sh daemon install-voice"
      ;;
    install-voice)
      # Voice owns :$GLOW_PORT — drop the bridge agent (and any manual bridge) first.
      mkdir -p "$LOG_DIR" "$RUN_DIR" "$la"
      if [ -e "$bdst" ]; then echo "removing bridge agent (voice owns :${GLOW_PORT}) ..."; _remove_agent "$bdst"; fi
      _free_manual bridge "$GLOW_PORT" bridge.pid
      _install_agent "$vsrc" "$vdst"
      echo "voice daemon installed (com.sonar.voice) — RunAtLoad + KeepAlive."
      echo "  ! EXPERIMENTAL: if macOS denies mic access under launchd (no prompt appears),"
      echo "    run 'scripts/sonar.sh voice' once from a terminal to grant Microphone, then"
      echo "    the agent should inherit it. Watch:  tail -f $LOG_DIR/voice.err.log"
      ;;
    uninstall)
      _remove_agent "$hdst"; _remove_agent "$bdst"; _remove_agent "$vdst"
      echo "harness + bridge + voice daemons removed. (Use 'scripts/sonar.sh up' for a manual stack.)"
      ;;
    status)
      local found; found="$(launchctl list 2>/dev/null | grep -E 'com\.sonar' || true)"
      if [ -n "$found" ]; then echo "$found"; else echo "no com.sonar agents loaded."; fi
      echo; cmd_status
      ;;
    *) echo "usage: sonar.sh daemon [install|install-voice|uninstall|status]" >&2; exit 1 ;;
  esac
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
    brief)       cmd_brief "$@" ;;
    daemon)      cmd_daemon "$@" ;;
    _exec-harness) cmd_exec_harness ;;   # hidden: launchd entrypoints (see plists)
    _exec-bridge)  cmd_exec_bridge ;;
    _exec-voice)   cmd_exec_voice ;;
    ""|-h|--help|help)
      sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
    *)
      echo "unknown command: $sub (try: up | down | restart | status | logs | ask | voice | google-auth | searxng | brief | daemon)" >&2
      exit 1 ;;
  esac
}

main "$@"
