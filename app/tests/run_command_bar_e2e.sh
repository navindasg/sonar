#!/usr/bin/env bash
# Compiles and runs the native command bar's end-to-end integration test against
# the LIVE stack. Skips (exit 2) rather than failing when the stack is not up —
# this needs a real bridge, a real harness and a GUI session, none of which are
# guaranteed, and a skip must never read as a pass.
#
#   app/tests/run_command_bar_e2e.sh
#
# NOTE: it takes over the keyboard for ~2s while it types into the bar, and it
# runs a real harness turn (local model inference), so give it up to ~2 minutes.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/../Sources/SonarApp"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

bridge_port="${SONAR_GLOW_PORT:-8770}"
harness_url="${SONAR_HARNESS_URL:-http://127.0.0.1:8787}"

if ! lsof -nP -iTCP:"$bridge_port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "SKIP: nothing is listening on :$bridge_port (start it with 'scripts/sonar.sh up')."
  exit 2
fi
if ! curl -fsS --max-time 5 "$harness_url/health" >/dev/null 2>&1; then
  echo "SKIP: harness not answering at $harness_url/health."
  exit 2
fi

# main.swift, because top-level code is only allowed in a file with that name.
mkdir -p "$out/e2e"
cp "$here/command_bar_e2e.swift" "$out/e2e/main.swift"

swiftc -O \
  "$src/CommandBarHTML.swift" \
  "$src/BridgeClient.swift" \
  "$src/CommandBarController.swift" \
  "$out/e2e/main.swift" \
  -o "$out/command_bar_e2e"

exec "$out/command_bar_e2e"
