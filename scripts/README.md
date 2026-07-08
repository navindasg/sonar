# scripts/

Setup + operational scripts.

- **`sonar.sh` — run the stack.** Starts/stops the harness (`:8787`) and overlay
  bridge (`:8770`) as **detached daemons** (nohup, orphaned into launchd) so they
  keep running independent of whatever launched them. Logs + pidfiles land under
  `$SONAR_HOME` (default `~/.sonar`), never in the repo or your vault.

  ```sh
  scripts/sonar.sh up            # start both (indexes the vault, warms the model)
  scripts/sonar.sh status        # ports, /health, resident Ollama models
  scripts/sonar.sh ask "..."     # one-shot question against the running harness
  scripts/sonar.sh logs          # tail -f both logs
  scripts/sonar.sh down          # stop both
  scripts/sonar.sh restart
  ```

  Config via env (all optional): `SONAR_PORT`, `SONAR_GLOW_PORT`,
  `SONAR_VAULT_PATH` (default `~/Documents/Obsidian Vault`), `SONAR_VAULT_NAME`,
  `SONAR_OLLAMA_URL`, `SONAR_HOME`. Requires `ollama serve` already running.

- **`hidutil` F5→F13 remap** — a LaunchAgent that remaps the dictation/F5 consumer key
  (`0x0000000C000000CF`) → F13 (`0x700000068`) at login, so the overlay can catch F13 with a plain global
  hotkey (no Accessibility prompt) and never fights system dictation. `hidutil` remaps are lost on
  reboot/keyboard reconnect → the LaunchAgent re-applies them. (Karabiner-Elements is the durable upgrade.)
- Future: install/uninstall of worker LaunchAgents, doctor/health checks.

**Status:** not written yet — part of spike **S1**.
