# Research — approaches, options, and what we borrow

_Synthesized 2026-07-06 from parallel recon (existing assets, signature UX, harness + inbox feasibility). Decision-oriented; sources inline._

---

## 1. Existing assets (confirmed on this machine)

- **osvoice** — `~/workspace/osvoice`, GitHub `navindasg/osvoice`, MIT. Python **3.12 only** (`>=3.12,<3.13`), FastAPI + uvicorn + Typer, WebSocket transport, MLX on-device (`mlx-lm`, `parakeet-mlx`, `mlx-audio`/Kokoro, `silero-vad`). Serves WS `/ws` + web client on **:9753**.
  - **LM seam:** `registry.resolve(slot, spec)` splits `scheme:rest` on the first colon. LLM adapters: `ollama`, `mlx`, `openai`. The **`openai:<base_url>#<model>`** adapter (`providers/llm_openai.py`) POSTs `{model, messages, stream:true}` to `<base_url>/chat/completions`, parses SSE deltas — pure async HTTP that **bypasses the capacity-1 `MLX_LIMITER`** (which serializes on-device evals). New LM = one class implementing `load/aclose/stream` + one registry line.
  - **Turn flow** (`pipeline.py`, one `Pipeline` per WS conn): transcribe → bounded history (8 turns) → cancellable generate-and-speak → clause aggregation → TTS → bounded PCM queue. `barge_in()` cancels + flushes (<60ms). VAD is *not* a registry slot — it endpoints frames ahead of STT.
  - **Reuse precedent:** `~/workspace/os-int` already vendored osvoice's pipeline and added `pipeline_tools.py` — proof the pipeline lifts into a tool-using product. **This is the template for the `voice/` LM-section rewrite.**
- **ObsidianRagMCP** — `~/obsidianragmcp`, GitHub `navindasg/ObsidianRagMCP`, MIT. Python 3.12+, **FastMCP** over **stdio only** (no ports), FAISS + Ollama embeddings. 7 tools: `search`, `read_note`, `list_notes`, `find_notes`, `note_context` (returns forward/backlinks), `vault_stats`, `reindex`. Per-vault index under `~/.obsidian-rag/<name>/`; multi-vault merge on `search`. Config `~/.obsidian-rag/config.yaml` (embed model `nomic-embed-text`, heading chunking, `top_k=5`). Nightly `format-daily` launchd worker already exists (a proto-worker). **Adaptation needed:** stdio → spawnable child or HTTP transport for the harness; fix vault path (real dir has a **space**: `~/Documents/Obsidian Vault`).
- **Ollama** — `gemma4:e4b-mlx` (9.6G), `gemma4:26b-mlx` (16G), `nomic-embed-text`, `mxbai-embed-large`, `llama3.2` all pulled. No new downloads needed.
- **iMessage bridge** — **not found** (only an aspirational portfolio blurb). Treat as non-existent → push via ntfy.

---

## 2. Signature UX — F5 → dark glow → voice

**Recommended P0 stack:** native Swift `LSUIElement` menu-bar agent that (1) remaps the dictation key upstream so F5 never triggers dictation, (2) catches the remapped key with an ordinary global hotkey, (3) draws a per-display click-through dark edge-glow via non-activating `NSPanel`, (4) talks to the Python pipeline over localhost WebSocket. Hammerspoon spike first to lock the look.

### Hotkey — the key insight: remap upstream, don't fight the tap
F5 on Apple Silicon laptops is a **dedicated dictation key** emitting HID consumer-page usage `0x000c00cf`, *not* a normal keycode — so naive keyDown taps may not even see it, and app-level hotkey APIs can't override system dictation.
- **Winner:** `hidutil` (or Karabiner-Elements for durability) remaps the dictation key → **F13**; the app registers a plain global hotkey on F13 (`KeyboardShortcuts`/Carbon `RegisterEventHotKey`) — **no Accessibility prompt**, no dictation race. `hidutil` remaps are lost on reboot/reconnect → persist via LaunchAgent. Karabiner survives reboot/sleep (installs a system extension).
- **Fallback (if you refuse to remap):** `CGEventTap` `.defaultTap` head-inserted, watching keyDown + `NSSystemDefined`, return `NULL` to swallow. Needs Accessibility, must restart app after granting, and re-signed binaries hit a silent-disable race. Fragile.
- Sources: [myByways F-key internals](https://mybyways.com/blog/remapping-physical-function-keys-on-macbook-pros) · [nanoant hidutil](https://www.nanoant.com/mac/macos-function-key-remapping-with-hidutil) · [Apple TN2450](https://developer.apple.com/library/archive/technotes/tn2450/_index.html) · [Karabiner docs](https://karabiner-elements.pqrs.org/docs/manual/configuration/configure-simple-modifications/) · [CGEvent tap silent-disable race](https://danielraffel.me/til/2026/02/19/cgevent-taps-and-code-signing-the-silent-disable-race/)

### Overlay — per-display, click-through, over full-screen apps
Borderless **`NSPanel`** with `.nonactivatingPanel` (never steals focus), `isOpaque=false`, `backgroundColor=.clear`, `ignoresMouseEvents=true`, `level=.screenSaver` (above menu bar → reaches the true screen edge/notch), `collectionBehavior=[.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]` (the `.fullScreenAuxiliary` flag floats it over full-screen apps for free — the exact thing web stacks need private APIs for). One panel per `NSScreen.screens`, full `frame` (not `visibleFrame`); re-layout on `didChangeScreenParametersNotification`; respect `safeAreaInsets` near the notch.
- **Glow:** Apple's Siri/Apple-Intelligence edge glow is a soft animated blurred gradient rim, reproducible in pure SwiftUI ([`jacobamobin/AppleIntelligenceGlowEffect`](https://github.com/jacobamobin/AppleIntelligenceGlowEffect)): inset stroke + angular gradient with animated stops + a blurred duplicate for bloom, `easeInOut.repeatForever()`. For the dark mood, invert the palette (near-black / charcoal / dim ember), high blur, thick rim/vignette. `CAGradientLayer` + `CABasicAnimation` is the cheapest GPU path; reserve Metal for organic noise (not needed at P0).
- Sources: [nonactivatingPanel](https://developer.apple.com/documentation/appkit/nswindow/stylemask-swift.struct/nonactivatingpanel) · [CollectionBehavior](https://developer.apple.com/documentation/AppKit/NSWindow/CollectionBehavior-swift.struct) · [translucent overlay guide](https://gaitatzis.medium.com/create-a-translucent-overlay-window-on-macos-in-swift-67d5e000ce90) · [notch/fullscreen notes](https://notes.alinpanaitiu.com/Fullscreen-apps-above-the-MacBook-notch)

### Build path — weighed
| Path | Hotkey | Overlay quality | Time-to-demo | Friction |
|------|--------|-----------------|--------------|----------|
| **(a) Native Swift** ✅ | Best | Best (real blur, per-screen, over-fullscreen free) | Days | Standard notarize; no TCC if you remap |
| **(b) Hammerspoon** (spike) | Good (+remap) | OK (`hs.canvas`) | **Hours** | Ships Hammerspoon, not a signed app |
| **(c) Tauri/Electron** | OK | Transparency-over-fullscreen needs **private API** → **bans MAS**; click-through needs 60fps cursor polling; `backdrop-filter` breaks | Medium | Real taxes for this exact UX |
Verdict: **(a) native for P0**, **(b) Hammerspoon for the ½-day aesthetic spike**, avoid (c).

### IPC — localhost WebSocket
State-push driven (`idle→listening→thinking→speaking`); WS gives server-push with sub-frame latency and a back-channel for `start/stop/cancel`. Envelope `{"state":"listening","level":0.7}` where `level` (mic RMS / TTS amplitude) modulates glow intensity for a breathing pulse. Unix-domain socket is the lower-surface alternative (what `local-whisper` uses). Avoid plain HTTP (can't push).
- Sources: [local-whisper](https://github.com/gabrimatic/local-whisper) · [Pipecat](https://medium.com/@bravekjh/building-voice-agents-with-pipecat-real-time-llm-conversations-in-python-a15de1a8fc6a)

---

## 3. Harness + tool-calling

- **Don't hand-roll the orchestration.** [`SecretiveShell/MCP-Bridge`](https://github.com/SecretiveShell/MCP-Bridge) (MIT, FastAPI) already exposes OpenAI `/v1/chat/completions`, is the MCP host, and runs the **tool-loop server-side** so the caller (osvoice) only sees a finished answer — exactly our design. **Caveat:** soft-deprecated (last release Feb 2025) → fork/reference and own it; rebuild the MCP-client side on the current official [`mcp` Python SDK](https://github.com/modelcontextprotocol/python-sdk). [LiteLLM](https://docs.litellm.ai/docs/mcp) is a good model-router to sit *behind* the harness but pushes tool execution to the client by default — not a drop-in host.
- **Tool-calling reliability:** Ollama's OpenAI shim supports `tools`/`tool_calls` and gemma is tool-capable, but there are **live streaming bugs** ([opencode #20995](https://github.com/anomalyco/opencode/issues/20995), [ollama #9941](https://github.com/ollama/ollama/issues/9941)) and ~15% JSON-format error rates at Q4; **Q8 is materially more reliable**. → **Hybrid:** native JSON on `26b` at **Q8** for tool turns, **XML-emitted-as-text + parse/repair auto-heal** fallback (esp. for `e4b`/streaming). Do tool-selection **non-streaming**, stream only the final answer. Sources: [analyticsvidhya gemma tool-calling](https://www.analyticsvidhya.com/blog/2026/04/gemma-4-tool-calling/) · [Morph XML tool calls](https://docs.morphllm.com/guides/xml-tool-calls) · [awesome-llm-json](https://github.com/imaurer/awesome-llm-json)

---

## 4. Email/calendar feasibility — the load-bearing finding

**The decisive M365 fact:** delegated `Mail.Read` / `Calendars.Read` are **NOT** low-impact permissions (that set is only `openid/profile/email/offline_access/User.Read`). Under Microsoft's default tenant setting, a normal user **cannot self-consent** to Mail/Calendar for a personally-registered app — it needs **admin consent**. Locked tenants (university, enterprise) also layer Conditional Access (managed-device/MFA) that can block device-code flow. Registering the app in your own tenant is fine; the block is at the **resource tenant's** consent gate. ([permission classifications](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-permission-classifications) · [user consent](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-user-consent) · [permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference))

**Google:** app passwords require 2FA on; Workspace admins can allowlist OAuth client IDs and Gmail `readonly` is a restricted scope; an External app in **Testing** issues refresh tokens that **die after 7 days** for non-basic scopes → personal Gmail via **IMAP + app-password** sidesteps the churn. ([LSA→OAuth](https://knowledge.workspace.google.com/admin/sync/transition-from-less-secure-apps-to-oauth) · [7-day token](https://www.unipile.com/google-oauth-refresh-token/))

### Per-account verdict (login-only, no admin)
| Inbox | Connectable? | Best path |
|-------|-------------|-----------|
| Personal Gmail | **Yes** | OAuth sign-in (start here per "sign-in based"); IMAP app-password is the churn-free fallback |
| Work Gmail (Workspace) | **Maybe** (~50/50, admin-dependent) | OAuth/IMAP if admin hasn't restricted client IDs |
| Work Outlook (generic M365) | **Likely NO** without admin consent | Ask IT to consent once, or forward-to-Gmail rule |
| Princeton Outlook | **Likely NO** (locked + Conditional Access) | OIT admin-consent request, or forward-to-Gmail |

**Bottom line:** 1 of 4 connects cleanly today (personal Gmail); work Gmail is admin-dependent; both Outlook mailboxes probably need an admin to consent once. **Design so email never hard-depends on Outlook; degrade to Gmail-only per account.**

### Servers to borrow, and split vs unified
**Lean: SPLIT.** [`softeria/ms-365-mcp-server`](https://github.com/softeria/ms-365-mcp-server) (multi-account, MSAL, 200+ Graph tools) for Outlook + a Google path ([`j3k0/mcp-google-workspace`](https://github.com/j3k0/mcp-google-workspace), [`navbuildz/gmail-mcp-server`](https://github.com/navbuildz/gmail-mcp-server)) for Gmail. Auth models differ fundamentally (MSAL/Graph vs Google OAuth/IMAP); splitting isolates the "likely-blocked M365" failure domain from the "works today" Gmail one. Keep an IMAP unifier ([`ai-zerolab/mcp-email-server`](https://github.com/ai-zerolab/mcp-email-server)) in reserve.

---

## 5. Quick calls

- **launchd** over cron (cron deprecated on macOS, silently skips sleep-missed jobs). [Apple scheduling](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html)
- **SQLite (WAL)** for live state, **outside** the vault (Obsidian's indexer and SQLite `-wal`/`-shm` fight). Durable notes stay as markdown in the vault. [sqlite when-to-use](https://sqlite.org/whentouse.html)
- **API search first** (Brave or Tavily) — single-user volume is far below the SearXNG self-host break-even; SearXNG captchas under bursts. [Brave API](https://brave.com/search/api/)
- **ntfy** for push (open-source, self-hostable, plain HTTP POST) over the fragile iMessage bridge. [ntfy.sh](https://ntfy.sh/)

## 6. Hardware / model split

- **M3 Max** (36–64GB): voice stack + `e4b` (~2–3GB, 80+ tok/s) comfortably; `26b` Q4 (~15GB, ~24–33 tok/s) with headroom on 64GB, tight on 36GB → run `26b` **on-demand**.
- **Base Mac mini 16GB: cannot serve `26b`** (~15GB weights alone → OOM/swap). Great for `e4b` + embeddings + scheduled workers.
- **Split:** M3 Max = voice + `e4b` + on-demand `26b`; mini = embeddings + workers + persistent `e4b`. For always-on `26b` off the Max, buy a ≥32GB mini.
- Sources: [26B needs 32GB+](https://dev.to/alanwest/how-to-get-gemma-4-26b-running-on-a-mac-mini-with-ollama-12hc) · [Apple-silicon benchmarks](https://sudoall.com/gemma-4-31b-apple-silicon-local-guide/)
