# Sonar — Claude Code project notes

Sonar is a local-first, on-device voice assistant for macOS: mic → STT → a local
tool-loop harness over Ollama/gemma → TTS, with an Obsidian vault as its RAG/graph.
Layout: `harness/` (Python `/v1` tool-loop), `voice/` (mic→STT→TTS loop + vendored
osvoice adapters), `spike/glow/` (Hammerspoon overlay), `infra/` (self-hosted
SearXNG for web.search), `scripts/sonar.sh` (launcher).

## Working agreement

- **Pushing is pre-authorized.** When a unit of work is done AND verified (tests
  green / builds pass / the change is actually exercised), just commit and push it —
  don't stop to ask "should I push?". Report what shipped.
- **Branch + PR flow.** The auto-mode safety classifier blocks pushing directly to
  `main`, so push a feature branch and open/update a PR — that path needs no
  approval. (To enable direct-to-`main` pushes, add a `git push` Bash permission
  rule in Claude settings.)
- Commit in logical chunks, conventional-commit style (`feat:`/`fix:`/`perf:`/…),
  matching the existing history. No attribution/co-author line (disabled globally).
- Audio / on-machine behavior (voice loop, overlay) is Navin's to acceptance-test:
  code-verify it, ship it, and flag what needs a live check — don't block the push
  on the manual test.
- **Relaunch the daemon after any change.** The harness/bridge/voice run as durable
  launchd agents (`com.sonar.*`) that load code at process start, so a change isn't
  live until the affected daemon is relaunched. Once a change is done, redeploy it:
  `scripts/sonar.sh daemon install` (harness + bridge) or `daemon install-voice`
  (voice); for `spike/glow/init.lua` overlay edits, `hs -c 'hs.reload()'`. Verify it
  came back up (`daemon status` / `/health` / overlay reconnected) — don't leave a
  verified change running against stale code.

## Run / verify

- Harness tests: `uv run --project harness pytest harness/tests -q`
- Voice tests: `cd voice && uv run --extra dev pytest -q`
- Stack up: `scripts/sonar.sh up` · voice: `scripts/sonar.sh voice` · web search:
  `scripts/sonar.sh searxng up` · Google consent: `scripts/sonar.sh google-auth`
