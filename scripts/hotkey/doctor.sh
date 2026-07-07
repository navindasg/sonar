#!/usr/bin/env bash
#
# doctor.sh — report the current hidutil remap state and explain how to
# confirm that F13 actually arrives when you press the dictation/F5 key.
#
set -euo pipefail

readonly SRC='0x0000000C000000CF'
readonly DST='0x700000068'
readonly PLIST_LABEL='com.sonar.hotkey-remap'
readonly INSTALLED_PLIST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"

main() {
  if ! command -v hidutil >/dev/null 2>&1; then
    echo "error: hidutil not found on PATH (expected on macOS)." >&2
    exit 1
  fi

  echo "== Sonar hotkey remap doctor =="
  echo
  echo "Expected mapping:"
  echo "  dictation/F5  Consumer page 0x0C usage 0xCF  (${SRC})"
  echo "        ->  F13  Keyboard page 0x07 usage 0x68  (${DST})"
  echo

  echo "Current UserKeyMapping (hidutil property --get):"
  local current
  current="$(hidutil property --get "UserKeyMapping" 2>/dev/null || true)"
  echo "${current}"
  echo

  # hidutil prints the codes as decimals: SRC=51539607759, DST=30064771176.
  if printf '%s' "${current}" | grep -q '51539607759' \
     && printf '%s' "${current}" | grep -q '30064771176'; then
    echo "STATUS: ACTIVE — the Sonar dictation->F13 remap is currently applied."
  else
    echo "STATUS: NOT ACTIVE — no Sonar remap in this session."
    echo "        Apply it now with ./remap.sh (or log in with the LaunchAgent installed)."
  fi
  echo

  echo "LaunchAgent (persists across login):"
  if [[ -f "${INSTALLED_PLIST}" ]]; then
    echo "  installed: ${INSTALLED_PLIST}"
    if launchctl list 2>/dev/null | grep -q "${PLIST_LABEL}"; then
      echo "  loaded:    yes"
    else
      echo "  loaded:    no (run ./install.sh, or launchctl load the plist)"
    fi
  else
    echo "  not installed (run ./install.sh to persist across reboots)"
  fi
  echo

  echo "How to confirm F13 actually arrives:"
  echo "  1. Open a key viewer that shows raw key codes, e.g.:"
  echo "       - Hammerspoon console:  hs.hotkey.bind({}, 'F13', function() hs.alert('F13!') end)"
  echo "       - Or the macOS Keyboard Viewer / any 'show key code' utility."
  echo "  2. Press the physical dictation/F5 key."
  echo "  3. With the remap ACTIVE you should see F13 (and dictation should NOT trigger)."
  echo "     With it NOT active you get the normal dictation behavior instead."
}

main "$@"
