# harness/CONTRACTS.md — the three shared interfaces

> Locked boundaries the parallel build streams build against. Change a shape here → announce it,
> because another stream depends on it. Ported/adapted from brook37 `daemon/tools/base.py`.

---

## 1. In-process `ToolBase` + `ToolRegistry` (harness-native tools)

Every harness-native tool is a `ToolBase` subclass declaring an **OpenAI/Anthropic-shaped** schema
plus `run(args, ctx)`. Field completeness is enforced at subclass-creation time (fails import, not
invocation). Sonar adaptation vs brook37: permission tiers collapse to `local` / `gated`
(browser/mutation → human-gated); no iMessage allowlist — `ToolContext` carries the per-turn state
handle, not a sender identity.

```python
Permission = Literal["local", "gated"]          # local = auto-run; gated = human approval

@dataclass(frozen=True)
class ToolContext:
    turn_id: str                                 # attributes step-events + telemetry to a turn
    state: State                                 # SQLite (WAL) conn for side-effect writes
    emit: Callable[[dict], None]                 # step-event sink (see §3)

class ToolBase(ABC):
    name:         ClassVar[str]                  # LLM-facing, unique in a registry
    description:  ClassVar[str]                  # LLM-facing
    input_schema: ClassVar[dict]                 # JSON Schema (object) for args
    permission:   ClassVar[Permission]
    def run(self, args: dict, ctx: ToolContext) -> str: ...   # returns text result
```

**`ToolRegistry` surface** (config-driven, pluggable — add a tool via config, never a core rewrite):

| Method | Contract |
|--------|----------|
| `ToolRegistry.load(*, tools=None, config_path=None)` | Build registry; read per-tool permission overrides from YAML (`config/tool_permissions.yaml`). Missing file → tool-declared defaults. |
| `schemas_for(ctx=None) -> list[dict]` | `[{name, description, input_schema}, ...]` — the `tools` array handed to the model. |
| `dispatch(name, args, ctx) -> str` | Invoke by name; raises `KeyError` (unknown) / `PermissionError` (gated + unapproved). Enforces the same gate as visibility. |
| `names() -> list[str]` | Registered tool names. |

Registration mirrors brook37 `default_tools()`: a module builds the default list; `.load()` wraps it.
RAG tools (§2) are reached as one MCP-backed `ToolBase` family, not reimplemented in-process.

---

## 2. RAG tool contract (stable boundary — exists today in `rag/src/obsidian_rag/tools.py`)

7 tools registered per `config.tools.enabled`. All take an injected FastMCP `ctx`; all return a
`dict` and, on failure, a structured `{"error", ...,"suggestion"}` dict (never raise to the caller).
`vault_name=None` means "first vault" for single-note reads, "all vaults" for searches. Signatures verbatim:

| Tool | Signature (minus `ctx`) | Returns (success) |
|------|-------------------------|-------------------|
| `search` | `search(query, vault_name=None, tags=None, folder=None)` | `{"results": [SearchResult...], "message"?}` |
| `read_note` | `read_note(path, vault_name=None)` | `{"path", "content", "frontmatter"}` |
| `list_notes` | `list_notes(path_prefix=None, vault_name=None)` | `{"notes": [{path, size, modified, tag_count}]}` |
| `find_notes` | `find_notes(query, vault_name=None)` | `{"results": [{file, heading_path}]}` |
| `vault_stats` | `vault_stats()` | `{"vaults": [{vault, note_count, chunk_count, index_age, embedding_model, last_reindex}], "total_notes", "total_chunks"}` |
| `reindex` | `reindex(vault_name)` | `{"status": "started"\|"already_running", "vault", "note_count"?, "message"}` |
| `note_context` | `note_context(path, vault_name=None)` | `{"note": {path, content}, "forward_links": [{path, exists}], "backlinks": [...]}` |

The vault is the knowledge graph: wikilinks = edges (`note_context`), tags = labels (`search` filter).
Vault is **read-only**; `reindex` is the only write and it only rebuilds the index. Embeddings via
Ollama — a down Ollama surfaces as `{"error": "Embedding failed — Ollama unreachable", ...}`.

---

## 3. Step-event contract (overlay "steps taken" panel)

The harness emits one compact JSON event per loop step via `ctx.emit`. The overlay streams these
(over the localhost WebSocket) to render an expandable timeline. Shape:

```json
{"step": "tool", "tool": "search", "detail": "<query>", "status": "ok"}
```

Fields: `step` (event kind, required), `tool` (tool name, on `tool`/`tool_result_summary`),
`detail` (short human string, ≤120 chars — the query, a summary, or a model id), `status`
(`ok` | `error` | `pending`, default `ok`). Optional `turn_id`, `ts` (epoch ms) may be attached
by the emitter for correlation. Events are additive; consumers ignore unknown fields/kinds.

**Event kinds:**

| `step` | When | `detail` example |
|--------|------|------------------|
| `turn_start` | Turn begins (one per user utterance) | `"weather in my notes?"` |
| `tool` | A tool call is dispatched | `"search"` (`detail` = args summary) |
| `tool_result_summary` | A tool returns | `"3 notes matched"` (`status=error` on failure) |
| `model_switch` | Router swaps model (e4b ↔ 26b) | `"e4b→26b (tool turn)"` |
| `final` | Answer begins streaming to TTS | `"streaming reply"` |

**Reading them back — `GET /events`.** Response is always `{"events": [...]}`, oldest-first, with
absent optional keys omitted (never serialized as `null`). Query params, all optional:
`turn_id` (one turn), `since` (inclusive epoch-ms floor — a poller passes back `last_ts + 1`),
`limit` (clamped 1–2000, default 100). Events are persisted (`event_store.py`), so a caller may
ask about a turn that ran before the current harness process. Persistence is best-effort by
design: a failed write costs history, never the turn.

---

## 3b. Attention surface (`GET /nudges`)

Silent and PULL-only — it answers because something asked, in that moment. Never speaks,
notifies, or schedules. Always 200, even on total failure (a menu bar has nowhere to render a
500); the failure payload is an EMPTY surface of the same shape.

```json
{"generated_at": "<iso8601>", "age_s": 0.0, "ttl_s": 60.0, "count": 1, "nudges": [
  {"id": "...", "source": "calendar|notes|email|todo",
   "severity": "high|medium|low", "rank": 0,
   "line": "<one-line human string>", "at": "<iso8601>"}]}
```

`id` is stable across polls, so a surface can own dismiss/snooze state locally. Ordering is a
total order: severity, then source in the fixed order above (time-boxed things the user cannot
recover if missed come first), then per-source `rank` lowest-first, then `id`. Unknown severities
and sources sort last rather than raising. `age_s`/`ttl_s` describe the TTL cache that keeps
polling cheap — a poll inside the TTL re-serves the same snapshot without touching any source.

---

_Adaptations vs brook37: local Ollama (XML-heal fallback for flaky JSON tool-calls), synchronous
per-request turn (no queue/supervisor/channels), vault-as-graph (no rdflib). Vault path quoted —
`~/Documents/Obsidian Vault` (has a space)._
