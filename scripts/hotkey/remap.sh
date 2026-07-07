#!/usr/bin/env bash
#
# remap.sh — apply the Sonar F5(dictation) -> F13 hidutil remap RIGHT NOW.
#
# This is the same command the LaunchAgent runs at login. Running it by hand
# is handy for testing without logging out. The remap is volatile: it is lost
# on reboot or when the keyboard reconnects, which is why install.sh sets up a
# LaunchAgent to re-apply it. See README.md for the HID usage-code rationale.
#
set -euo pipefail

# --- HID usage codes (see README.md "Why these codes") -----------------------
# Source: the MacBook dictation key (physically F5 on Apple-silicon laptops)
#   emits Consumer page (0x0C) usage 0xCF ("Voice Command" / AC Dictation),
#   NOT a normal keyboard keycode. In hidutil's 64-bit form the high 32 bits
#   are the usage page and the low 32 bits are the usage:
#     0x0000000C000000CF  ==  page 0x0C, usage 0xCF   (= 0xC000000CF, dec 51539607759)
# Destination: F13 on the Keyboard/Keypad page (0x07), usage 0x68:
#     0x0000000700000068  ==  0x700000068
readonly SRC='0x0000000C000000CF'
readonly DST='0x700000068'

readonly MAPPING="{\"UserKeyMapping\":[{\"HIDKeyboardModifierMappingSrc\":${SRC},\"HIDKeyboardModifierMappingDst\":${DST}}]}"

main() {
  if ! command -v hidutil >/dev/null 2>&1; then
    echo "error: hidutil not found on PATH (expected on macOS)." >&2
    exit 1
  fi

  if ! hidutil property --set "${MAPPING}" >/dev/null; then
    echo "error: hidutil failed to apply the UserKeyMapping." >&2
    echo "       (No sudo is required for user key mapping — check the JSON.)" >&2
    exit 1
  fi

  echo "Applied remap: dictation/F5 (${SRC}) -> F13 (${DST})."
  echo "Verify with:   hidutil property --get \"UserKeyMapping\""
  echo "Or run:        ./doctor.sh"
}

main "$@"
