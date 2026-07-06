> **This is a living draft.** It captures the original design conversation and is meant to be
> argued with and rewritten. For what's since been provisionally decided, see
> [`DECISIONS.md`](DECISIONS.md); for the research behind those calls, see [`RESEARCH.md`](RESEARCH.md).
> Codename is now **Sonar** (locked 2026-07-06); the rest of this draft stands as written.

---

# ⚠️ DRAFT — NO LOCKED-IN CHOICES

This is an early **draft PRD** for discussion only. **Nothing here is decided.**
Every tool, model, framework, structure, name, and phase below is a **candidate**,
not a commitment. Where a choice is named, read it as "leaning toward / one option,"
never "chosen." Real forks live in [§10 Open decisions](#10-open-decisions) and are
deliberately left unresolved. Expect this to change substantially as we build.

---

# Ambient Local Assistant — PRD (Draft v0.1)

**Working title:** Ambient Local Assistant
**Codename:** Sonar (locked 2026-07-06; was TBD / placeholder *Oracle*)
**Author:** Navin
**Status:** Draft — brainstorming synthesis, no approvals, no locked choices
**Last updated:** 2026-07-06

---

## How to read this draft

This document synthesizes a design conversation into a working shape. It is meant to
be edited, argued with, and largely rewritten. Treat every recommendation as a starting
proposal. Two conventions:

- **(candidate)** — a named option that seems reasonable but is not selected.
- **(open)** — a genuine fork with no current preference; see §10.

If a section reads as confident, that is a drafting artifact, not a decision.

---

## 1. Summary

A personal, always-on assistant that runs entirely on local hardware and helps across
the whole day at near-zero friction. It is **mostly deterministic plumbing with a smart,
conversational face** — not a free-running autonomous agent. Scheduled jobs do bounded
work on a timer and write to a shared state store; a conversational layer (voice + a
dashboard UI) reads that state, answers questions, and can trigger jobs on demand. The
language model is used only as a **leaf function** inside jobs (classify, draft, summarize)
and as the **interface**, never as the pilot deciding what to do next.

The value is not that it can do things a summoned tool (Claude Code, Cowork, an
automation builder) cannot — it is that it does them ambiently: already running,
holding continuity across the day, and reachable from wherever you are.

---

## 2. Motivation

Summoned tools are powerful but you must *go to* them, they have no memory of your day,
and they vanish when the tab closes. An ambient assistant differs in three ways, and
those differences generate every use case:

- **Presence** — already running, so the cost of using it is ~zero. No app to open, no
  context to re-establish.
- **Continuity** — it holds the thread of the day/week: which client, what was open,
  what you said you'd follow up on.
- **Ambient context** — it can see current state (calendar, inbox, notes) so you rarely
  have to explain yourself.

Concrete pains this targets: constant context-switching across concurrent client work,
losing stray thoughts/TODOs mid-flow, and reassembling "what's going on today" across
several tabs every morning.

---

## 3. Goals / Non-goals

### Goals
- Fully local brain: no audio or text required to leave the machine for core operation.
  (Web search is the one unavoidable exception — see §10.)
- Zero-friction capture and recall across the day.
- A reliable morning/any-time brief.
- Multi-account email + calendar awareness with **suggested replies as drafts** the user
  approves — never autonomous sending.
- Voice-first, with a dashboard for glanceable monitoring.
- Extensible: new use cases (the ~40% still undefined) should be **additive** — a new
  worker or tool, not a core rewrite.

### Non-goals (for now)
- **Code editing / dev work** — that stays with Claude Code + the user. Explicitly out.
- **A free-running autonomous agent loop** — rejected in favor of scheduled deterministic
  jobs + a conversational layer. (Prior experience: the loose-harness pattern is flakier.)
- **Cloud dependence for the brain.**
- **Multi-user / productization.** Personal infrastructure first. (A client-isolated
  "cockpit" productization is noted as possible future, not in scope.)

---

## 4. User

One user (Navin). AI/ML consultant on Apple Silicon, comfortable running local models,
building MCP servers, and maintaining personal infrastructure. Already operates a
Mac-mini home-agent reachable via iMessage. Values the local property highly and prefers
deterministic, debuggable systems over autonomous magic.

---

## 5. Design principles (proposed)

1. **Deterministic spine, LLM at the leaves.** Schedules and control flow are plain code.
   The model classifies/drafts/summarizes and serves as the interface. It never chooses
   its own next action in a loop.
2. **Local-first.** Prefer on-device everything; treat any network hop as a deliberate
   exception to justify.
3. **Consequence-tiered permissions.** Actions are gated by how much damage they can do,
   not by category. Read = silent; write = confirm; act = always confirm + scoped.
4. **Reuse what exists.** Two hard pieces are already built (memory/RAG and voice); build
   only the harness, workers, policy layer, and UI.
5. **Additive extensibility.** The unknown future use cases attach as new workers/tools.

---

## 6. Architecture (overview)

Three layers, plus a tool-exposure model.

### 6.1 Layers
- **Deterministic workers (scheduled).** launchd/cron scripts (candidate). Examples:
  `email-poll` (classify + draft suggested replies), `calendar-sync`, `brief-builder`,
  and misc `watchers` (training runs, market, etc.). Each may call the LLM as a bounded
  leaf step, but its control flow is plain code.
- **State store.** A fast, disposable live-state layer (SQLite — candidate) plus the
  **Obsidian vault** as long-term/semantic memory. Briefs and decisions get written back
  into the vault as notes, so the assistant's memory and the user's second brain are the
  same store, and today's briefs become tomorrow's RAG context.
- **Interface.** The conversational layer (custom harness), voice I/O, and a dashboard UI.
  Reads state, presents briefs and review lanes, and can dispatch workers on demand.

### 6.2 Tool exposure model
Everything is a **local MCP server**; the **custom harness is the MCP host**; **osvoice
fronts the harness**. The seam that makes this cheap: osvoice's LM slot accepts an
OpenAI-compatible endpoint (`openai:<base_url>#<model>`), so the harness exposes an
OpenAI-compatible `/v1/chat/completions` endpoint and *looks like a model* to osvoice.
Voice-in → osvoice STT → harness (with tools + memory) → osvoice TTS → voice-out, no
rebuild of the voice stack. Scheduled workers are **MCP clients of the same servers**, so
they and the harness share one tool layer.

---

## 7. Scope — features

Selected in discussion; each is a candidate in shape, not implementation.

- **Brief (morning / any-time).** `brief-builder` assembles from live state + vault +
  external signals (training runs, market) and writes the brief back into Obsidian.
  "Any brief" is the same worker with a different time window.
- **Email triage → suggested reply.** `email-poll` pulls unread across all accounts,
  classifies (client / noise / needs-you), and for client mail drafts a "here's what they
  want + suggested reply." Lands as a **draft** (syncs to phone) or a review-lane item.
  **Never auto-sends.**
- **Calendar.** Multi-account awareness; personal read/write, other accounts read-only
  (scoping enforced by the harness policy layer — see §8).
- **Voice interface.** osvoice, fronting the harness. Target ~700–800 ms voice-to-voice
  with barge-in (subject to the latency approach in §10).
- **Console UI.** A dark, dense "command console" dashboard (batcave direction, explicitly
  *not* Iron Man) with live tiles: inbox lane, merged calendar, the brief, open loops,
  worker/training status. This is a *monitor you occasionally talk to*, not a chat window.
- **Push / notifications.** Workers reach the user unprompted (brief delivery, "email from
  client X"). Candidate channel: the existing iMessage bridge.
- **Web search.** A read tool for the harness and workers.
- **Browser automation.** Playwright, to take actions on the user's behalf — the one
  genuinely dangerous capability; heavily gated (see §8, §9).

---

## 8. Tool surface & permission tiers (proposed)

Most MCP servers expose their full capability regardless of intent, so the **tier is
enforced in the harness policy layer**, keyed on tool and (for calendar/email) account.

| Tier | Confirm? | Tools (candidates) |
|------|----------|--------------------|
| **Read** | none | web-search, email read, calendar read, obsidian-rag read |
| **Write** | confirm | email *draft* (never send), calendar *personal* write, note write* |
| **Act** | always confirm + scoped | playwright, email *send* (out of scope for now) |

\* Obsidian note-write (e.g. appending the daily brief) is low-risk and self-owned; it is
a **candidate for auto (no confirm)** rather than the write tier. (open)

Account scoping example: calendar write is permitted only for the personal alias; all
other aliases are read-only. This split is a **harness rule**, not a server capability.

---

## 9. Component inventory

| Component | Status | Notes |
|-----------|--------|-------|
| Memory / RAG | **HAVE** | `ObsidianRagMCP` — FAISS + Ollama embeddings, wikilink context, multi-vault, file-watch, configurable tool surface, nightly daily-note formatter (already a proto-worker) |
| Voice I/O | **HAVE** | `osvoice` — MLX voice-to-voice, silero-VAD, barge-in, provider-agnostic LM slot |
| Model | **HAVE** | gemma4 via Ollama — `e4b` (fast, voice), `26b` (heavier reasoning) |
| Push | **HAVE / adapt** | existing iMessage bridge (candidate) |
| Harness | **BUILD** | custom; OpenAI-compatible `/v1` endpoint + MCP host + policy layer |
| Workers | **BUILD** | launchd/cron scripts (candidate) |
| State store | **BUILD** | SQLite (candidate) + Obsidian |
| Console UI | **BUILD** | batcave dashboard (framework open) |
| Email/calendar MCP | **ADOPT** | unified multi-account server *or* split gmail + ms365 (open) |
| Web-search MCP | **ADOPT** | SearXNG self-hosted *or* Brave/Tavily API (open) |
| Browser MCP | **ADOPT** | `@playwright/mcp` (candidate) |

---

## 10. Open decisions

**None of these are decided.** Listed with options and current lean (if any) only.

1. **Email/calendar server.** One unified multi-account server (covers 2 Gmail + 2 Outlook
   + calendar) vs. split (gmail-multi + ms365). Trade-off: single dependency & unified
   routing vs. maturity per provider. *No selection.*
2. **Email write posture.** Read-only vs. read + draft-only (never send). Draft-only keeps
   the suggested-reply feature and still forbids autonomous sending. *Leaning read + draft;
   not decided.*
3. **Web search locality.** SearXNG self-hosted (max locality, more maintenance) vs.
   Brave/Tavily API (zero maintenance, query leaves via a vendor). *No selection.*
4. **Voice latency vs. agentic depth.** Fast/slow split (small model answers chit-chat
   directly; escalate to the tool-using harness only when needed) vs. single harness path
   that streams an immediate spoken ack and runs tools async. *Must decide before harness
   request-flow is written.*
5. **Model routing.** When to use `e4b` vs `26b`. *Open.*
6. **Tool-call format for gemma.** Native OpenAI JSON function-calling vs. XML-style tool
   calls emitted as text + parse/repair ("auto-heal"). Small local models are unreliable
   at strict JSON. *Leaning XML-heal; not decided.*
7. **State store shape.** SQLite vs. flat files vs. both; what's ephemeral (SQLite) vs.
   permanent (vault). *Open.*
8. **Console framework.** Local web app; framework and whether it embeds the voice client
   is open.
9. **Push channel.** iMessage bridge vs. ntfy/Pushover vs. console-only. *Leaning iMessage;
   not decided.*
10. **Codename.** Placeholder only.

---

## 11. Risks & mitigations (draft)

- **Prompt injection via untrusted email into action tools.** A malicious email could try
  to drive playwright or a send. *Mitigation:* consequence tiers; the **email→act path is
  always human-gated**; no auto-send, ever.
- **Voice latency vs. tool-using turns.** Full agent turns are multi-second. *Mitigation:*
  the fast/slow routing in §10.4.
- **MLX concurrency.** osvoice notes MLX is not thread-safe; STT/LLM/TTS funnel through a
  single capacity-1 limiter, while network backends bypass it. *Implication:* run the
  harness LLM as a **network backend (Ollama / OpenAI-compatible)** so it doesn't contend
  with STT/TTS on the Metal backend.
- **Local model tool-call reliability.** *Mitigation:* XML-heal (§10.6).
- **Scope creep / undefined ~40%.** *Mitigation:* additive worker/tool model so new needs
  don't touch the core.
- **Maintenance burden / single point.** It's personal infra; accept, but keep pieces
  independently restartable.

---

## 12. Suggested phasing (illustrative, reorderable — not a commitment)

- **P0 — Prove the seam.** Harness skeleton (OpenAI `/v1` + MCP host) → wire `obsidian-rag`
  → osvoice points its LM slot at the harness → add one read tool (web search). Goal: talk
  to a tool-using, memory-having harness by voice.
- **P1 — Brief + inputs.** Adopt email/calendar MCP (read); `brief-builder`; iMessage push.
  Goal: a working morning brief.
- **P2 — Suggested replies + policy layer.** `email-poll` drafting; review lane; the
  consequence-tier enforcement + account scoping.
- **P3 — Console.** Batcave dashboard; calendar write (personal-scoped).
- **P4 — Actuation + expansion.** Playwright with guardrails; new workers for the
  undefined use cases as they surface.

---

## 13. Success signals (qualitative, personal)

- Zero-friction capture is *actually used* daily (the real test of ambient value).
- The morning brief replaces manual multi-tab assembly.
- "What was I doing for client X?" reliably recalls across days.
- Voice stays snappy for ordinary turns.
- **Nothing is ever sent or acted on without explicit approval.**
- New use cases land as workers/tools without touching the core.

---

## 14. Glossary

- **Harness** — the custom brain: an OpenAI-compatible endpoint that is also an MCP host,
  with the policy layer. What osvoice and the console talk to.
- **Worker** — a scheduled deterministic script that does bounded work and writes state.
- **Leaf call** — a single, bounded LLM invocation inside a worker (classify/draft/summarize).
- **Policy layer** — the harness component that enforces permission tiers and account scoping.
- **State store** — SQLite (live/ephemeral) + Obsidian vault (long-term/semantic).

---

> *Reminder: this is a draft. Nothing above is locked in. Edit freely.*
