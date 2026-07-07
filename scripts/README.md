# scripts/

Setup + operational scripts.

- **`hidutil` F5→F13 remap** — a LaunchAgent that remaps the dictation/F5 consumer key
  (`0x0000000C000000CF`) → F13 (`0x700000068`) at login, so the overlay can catch F13 with a plain global
  hotkey (no Accessibility prompt) and never fights system dictation. `hidutil` remaps are lost on
  reboot/keyboard reconnect → the LaunchAgent re-applies them. (Karabiner-Elements is the durable upgrade.)
- Future: install/uninstall of worker LaunchAgents, doctor/health checks.

**Status:** not written yet — part of spike **S1**.
