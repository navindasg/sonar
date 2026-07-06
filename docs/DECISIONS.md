# Decisions — provisional log

> **You (Navin) are the source of truth, not this file.** Every row is a *current lean*, not a
> commitment. To change one: edit the row, drop a line in the changelog at the bottom, and move on —
> no ceremony. Decisions marked **OPEN** are genuine forks with no pick yet.

_Last updated: 2026-07-06._

## Locked-ish (provisional)

| Area | Decision | Rationale / notes |
|------|----------|-------------------|
| **Name** | **Sonar** | Lucius Fox's sonar system in *The Dark Knight* — ambient sensing that surfaces everything. Deniable, evocative of "listening." |
| **Repo** | **Public**, single **monorepo**; existing repos **vendored + adapted** (upstream = reference) | Secrets in Keychain/`~/.config/sonar`, never committed. `.gitignore` blocks `.env`/tokens/`*.sqlite`/indexes. |
| **Models** | `e4b` = voice/chit-chat/routing · `26b` = reasoning/tools (Q8, on-demand) | Tool-selection turns run **non-streaming**; only the final answer streams to voice. Both already pulled in Ollama. |
| **Tool-calling** | native JSON `tool_calls` primary + **XML-emitted-as-text → parse/repair auto-heal** fallback | Small-model JSON is flaky, worst on streaming turns. Tool set is **config-driven / pluggable**. |
| **Voice** | `osvoice` **vendored + adapted** — LM section rewired to the lightweight harness | Runs on M3 Max (MLX). Network-LLM path bypasses the capacity-1 MLX limiter. |
| **UX** | Native Swift `LSUIElement` menu-bar app; **F5→F13 `hidutil` remap**; per-display non-activating `NSPanel` dark glow; localhost **WebSocket** IPC | Don't fight dictation at the tap layer — remap upstream, catch F13 with a plain hotkey (no Accessibility prompt). |
| **Email/cal** | **OAuth sign-in, provider-generic (Gmail + Outlook)**; **split** MCP servers (Google path + `softeria/ms-365-mcp-server`); **draft-only, never auto-send** | Try Outlook login-only; if a tenant needs admin consent, **skip it → Gmail-only fallback**. Never hard-depend on Outlook. |
| **State** | **SQLite (WAL)** for live/ephemeral state, **outside** the vault; Obsidian vault = durable/RAG | Vault path `~/Documents/Obsidian Vault` (**has a space** — RAG sample config omits it; must fix). |
| **Scheduling** | **launchd** LaunchAgents | cron is deprecated on macOS and silently skips jobs missed during sleep. |
| **Search** | **Brave or Tavily API** first; SearXNG self-host later | Single-user volume is far below the self-host break-even. |
| **Push** | **ntfy** | iMessage bridge doesn't exist yet and is fragile/ToS-gray. |
| **Hardware** | M3 Max = voice + `e4b` + on-demand `26b` · Mac mini 16GB = embeddings + workers + persistent `e4b` | 16GB mini **cannot** serve `26b` (~15GB weights alone). |
| **Browser** | Playwright MCP, **Act-tier, always human-gated** | The one genuinely dangerous capability. Deferred to I4. |

## OPEN — decide as spikes land

- **Harness structure** — fork the `SecretiveShell/MCP-Bridge` pattern vs a thinner bespoke server vs other. Hard reqs regardless: **lightweight** + **config-driven pluggable tools**. → resolve during **S3**.
- **Depth of osvoice LM-section rewrite** — how much of the pipeline becomes tool-aware. → S2 → S4.
- **Web-search vendor** — Brave vs Tavily.
- **Personal Gmail** — OAuth vs IMAP app-password. Start OAuth (per "sign-in based"); app-password is the fallback if the External/Testing 7-day refresh-token churn annoys.
- **Remap tool** — start `hidutil` (zero-install LaunchAgent); Karabiner-Elements is the durable upgrade.
- **Dashboard framework** — TBD at I3.
- **Always-on `26b`** — whether to buy a ≥32GB Mac mini.

## Build method — spike-first

Prove each piece independently before wiring anything together. Keep the spikes that stick; swap the ones that don't; integrate last.

**Spikes:** `S1` glow+hotkey (Hammerspoon) · `S2` voice loop · `S3` harness seam (`/v1`+MCP→RAG) · `S4` voice↔harness · `S5` email OAuth · `S6` worker+state.
**Integrations:** `I0` = S1+S4 (press F13 → dark glow → tool-using, memory-having voice reply) · `I1` brief+inputs · `I2` suggested replies + policy layer · `I3` console · `I4` actuation + expansion.

## Changelog

- **2026-07-06** — Initial provisional set from the planning session. Name locked to *Sonar*. Repo strategy = vendor-into-monorepo. Harness structure left OPEN. iMessage push dropped in favor of ntfy (no existing bridge). Full research in [`RESEARCH.md`](RESEARCH.md).
