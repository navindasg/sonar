# Sonar

> Ambient, local-first voice assistant for macOS. Press a key, the screen gets a dark glow, and a voice that already knows your day answers.

Named for the surveillance sonar Lucius Fox built in *The Dark Knight* — ambient sensing that turns background signal into a picture of exactly what you need. Dark-Knight **mood**, not costume: no logos, nothing loud.

## What it is

Mostly **deterministic plumbing with a conversational face**. Scheduled workers do bounded work on a timer and write to a shared state store; a custom **harness** (LLM at the leaves, never the pilot) reads that state and answers by voice — with a dark dashboard for glanceable monitoring. Everything runs on-device (Apple Silicon); the only thing that leaves the machine is web search.

- **Local brain** — gemma via Ollama (`e4b` fast/voice, `26b` reasoning), MLX voice I/O.
- **Voice-first** — hotkey → dark screen-edge glow → talk. Barge-in supported.
- **Ambient** — already running, holds continuity across the day, sees your calendar/inbox/notes so you rarely have to explain yourself.
- **Safe by construction** — consequence-tiered permissions; email is **draft-only, never auto-sent**.

## Layout

| Dir | Purpose |
|-----|---------|
| `docs/` | [`PRD.md`](docs/PRD.md) (living draft) · [`DECISIONS.md`](docs/DECISIONS.md) (provisional decision log — **you** override) · [`RESEARCH.md`](docs/RESEARCH.md) (findings + sources) |
| `harness/` | OpenAI-compatible `/v1` + MCP host + policy/model-router. **Structure open** (see DECISIONS). |
| `voice/` | Vendored + adapted `osvoice` (MLX STT · VAD · TTS · LM). LM section gets rewired to the harness. |
| `rag/` | Vendored + adapted `ObsidianRagMCP` (FastMCP · FAISS · Ollama). |
| `overlay/` | Native Swift menu-bar app: F13 hotkey + per-display dark-glow `NSPanel` + WS client. |
| `spike/` | Throwaway prototypes (Hammerspoon glow, etc.) to lock look/feel before committing. |
| `workers/` | launchd deterministic workers (`brief-builder`, `email-poll`, …). |
| `mcp/` | Configs + thin wrappers for adopted MCP servers (google, ms-365, web-search, playwright). |
| `state/` | SQLite (WAL) live/ephemeral state. Lives **outside** the Obsidian vault. |
| `scripts/` | Setup: `hidutil` F5→F13 remap LaunchAgent, etc. |
| `config/` | Templated config + `.env.example`. **No secrets in the tree.** |

## Status

Foundation scaffold seeded 2026-07-06. Build follows a **spike-first** method — prove each piece independently, keep what sticks, integrate last. See [`docs/DECISIONS.md`](docs/DECISIONS.md) §Build for the spike list (S1–S6 → I0–I4).

## Secrets

Public repo. OAuth tokens, client secrets, and app-passwords live in the **macOS Keychain** or `~/.config/sonar/` — **never committed**. `.gitignore` blocks `.env`, `*.sqlite*`, tokens, and index artifacts.
