# Vendored: ObsidianRagMCP

Source-only copy of **ObsidianRagMCP**, vendored into Sonar to be adapted (not used as-is).

- **Upstream:** `git@github.com:navindasg/ObsidianRagMCP.git`
- **Vendored at commit:** `fbe998f`
- **Vendored on:** 2026-07-06
- **Excluded:** `.git`, `.venv`, caches, `.claude`, `.planning`.

## Why it's here / what changes

Provides memory/RAG over the Obsidian vault (FastMCP · FAISS · Ollama embeddings; 7 tools
incl. `search`, `note_context`). Two known adaptations for Sonar:

1. **Transport** — upstream hard-codes stdio (`server.run()`). The harness will either spawn
   it as a child MCP process or switch FastMCP to an HTTP/SSE transport.
2. **Vault path** — the real vault is `~/Documents/Obsidian Vault` (**contains a space**); the
   upstream sample config uses `~/Documents/ObsidianVault`. Config must point at the real path.

The nightly `format-daily` launchd worker is a proto-worker worth keeping. Embedding model
`nomic-embed-text` is already pulled in Ollama.
