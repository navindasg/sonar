#!/usr/bin/env bash
#
# uninstall.sh — remove the Sonar remap LaunchAgent and restore default keys.
#
# Unloads and deletes ~/Library/LaunchAgents/com.sonar.hotkey-remap.plist, then
# clears the active mapping so the dictation/F5 key works normally again right
# away (no logout needed).
#
set -euo pipefail

readonly LABEL='com.sonar.hotkey-remap'
readonly DEST_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

main() {
  if [[ -f "${DEST_PLIST}" ]]; then
    launchctl unload "${DEST_PLIST}" >/dev/null 2>&1 || true
    rm -f "${DEST_PLIST}"
    echo "Removed LaunchAgent: ${DEST_PLIST}"
  else
    echo "No installed LaunchAgent found at ${DEST_PLIST} (nothing to remove)."
  fi

  # Clear the active session mapping so keys behave normally immediately.
  if command -v hidutil >/dev/null 2>&1; then
    hidutil property --set '{"UserKeyMapping":[]}' >/dev/null 2>&1 || true
    echo "Cleared active hidutil user key mapping for this session."
  fi

  echo "Done. The dictation/F5 key is back to default behavior."
}

main "$@"
