#!/usr/bin/env bash
#
# unmap.sh — remove the Sonar remap and restore default key behavior NOW.
#
# This clears ALL hidutil user key mappings for the current session by setting
# an empty mapping list. After this the dictation/F5 key behaves normally again
# until the next login (if the LaunchAgent is installed) or until you re-run
# remap.sh. To stop it re-applying at login entirely, run uninstall.sh.
#
set -euo pipefail

main() {
  if ! command -v hidutil >/dev/null 2>&1; then
    echo "error: hidutil not found on PATH (expected on macOS)." >&2
    exit 1
  fi

  if ! hidutil property --set '{"UserKeyMapping":[]}' >/dev/null; then
    echo "error: hidutil failed to clear the UserKeyMapping." >&2
    exit 1
  fi

  echo "Cleared all hidutil user key mappings for this session."
  echo "Note: this only affects the running session. If the LaunchAgent is"
  echo "      installed, the remap returns at next login — run ./uninstall.sh"
  echo "      to remove it permanently."
}

main "$@"
