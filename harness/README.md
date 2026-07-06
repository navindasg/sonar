# harness/

The brain: an **OpenAI-compatible `/v1/chat/completions`** server that is also an **MCP host**,
with a **policy/permission layer** and an **e4b↔26b model router**. osvoice's LM slot points here;
scheduled workers share the same tool layer.

**Status:** not built yet. **Structure is an OPEN decision** (see `docs/DECISIONS.md`) — candidate
reference is the `SecretiveShell/MCP-Bridge` pattern (server-side tool-loop). Hard requirements
regardless: **lightweight** (fronts a snappy voice loop) and **config-driven / pluggable tools**
(add any MCP server via config, never a core rewrite).

First proof = spike **S3** (stream a `/v1` completion that triggers a `rag.search`).
