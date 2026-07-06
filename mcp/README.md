# mcp/

Configs + thin wrappers for **adopted** MCP servers the harness hosts. Tools are pluggable — adding
a capability is a config entry, not a core change.

Planned adoptions:
- **Google** (Gmail + Calendar) — OAuth sign-in; e.g. `j3k0/mcp-google-workspace` / a Gmail MCP.
- **Microsoft 365** (Outlook + Calendar) — `softeria/ms-365-mcp-server` (multi-account, MSAL).
  Degrades gracefully: if a tenant needs admin consent, that account is skipped (Gmail-only fallback).
- **web-search** — Brave or Tavily API (read tool).
- **playwright** — `@playwright/mcp`, Act-tier, **always human-gated** (deferred to I4).

The local `rag/` (ObsidianRagMCP) is spawned as a child MCP server (see `rag/VENDORED.md`).

**Status:** configs land as spikes S5 (email) and I-phases proceed. **Never commit tokens** — auth
lives in Keychain / `~/.config/sonar`.
