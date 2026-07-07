#!/usr/bin/env bash
#
# install.sh — install the Sonar remap LaunchAgent so the F5->F13 remap is
# re-applied automatically at every login.
#
# Copies com.sonar.hotkey-remap.plist into ~/Library/LaunchAgents and loads it.
# Loading it also applies the remap immediately (RunAtLoad), so you do NOT need
# to log out to start using F13. No sudo required — this is a per-user agent.
#
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LABEL='com.sonar.hotkey-remap'
readonly SRC_PLIST="${SCRIPT_DIR}/${LABEL}.plist"
readonly AGENTS_DIR="${HOME}/Library/LaunchAgents"
readonly DEST_PLIST="${AGENTS_DIR}/${LABEL}.plist"

main() {
  if [[ ! -f "${SRC_PLIST}" ]]; then
    echo "error: plist not found next to this script: ${SRC_PLIST}" >&2
    exit 1
  fi

  if ! plutil -lint "${SRC_PLIST}" >/dev/null; then
    echo "error: ${SRC_PLIST} failed plutil validation; refusing to install." >&2
    exit 1
  fi

  mkdir -p "${AGENTS_DIR}"
  cp "${SRC_PLIST}" "${DEST_PLIST}"
  echo "Copied plist -> ${DEST_PLIST}"

  # Reload cleanly: unload any prior copy (ignore errors), then load.
  launchctl unload "${DEST_PLIST}" >/dev/null 2>&1 || true
  if ! launchctl load "${DEST_PLIST}"; then
    echo "error: launchctl load failed for ${DEST_PLIST}." >&2
    exit 1
  fi

  echo "Loaded LaunchAgent ${LABEL} (remap applied now and at every login)."
  echo "Confirm with: ./doctor.sh"
}

main "$@"
