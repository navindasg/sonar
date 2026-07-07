# harness/

The brain: an **OpenAI-compatible `/v1/chat/completions`** server that runs a
bounded tool-use loop over **local gemma (Ollama)** and answers with tools +
retrieved memory. osvoice's LM slot points here (the STT↔TTS seam); the SSE
shape matches `voice/osvoice/providers/llm_openai.py` byte-for-byte.

Ported from the battle-tested `AI-Dasgupta/brook37` agent loop. See
`docs/DECISIONS.md` and `harness/CONTRACTS.md`.

## Status — skeleton built & proven headlessly (Stream A)

- `POST /v1/chat/completions` (SSE) → non-streaming tool loop → streamed answer.
- Config-driven, pluggable `ToolRegistry` (`sonar_harness/tools/base.py`). Wired
  this pass: `rag.search`, `rag.note_context` (real), `state_read`, `todo_add`
  (stub).
- Native JSON `tool_calls` primary + **XML-heal** fallback (`toolcall.py`); the
  gemma spike found both e4b and 26b emit clean tool calls, so heal is a
  defensive fallback, not the primary path.
- Model router (`model_router.py`): fast `e4b` selects tools; the final grounded
  synthesis escalates to `26b` when tools were used (`config/models.yaml`).
- Step-events at `GET /events` for the overlay "steps taken" panel
  (`events.py`, shape in `CONTRACTS.md §3`).

Run: `SONAR_PORT=8787 uv run --project harness python -m sonar_harness`
Tests: `uv run --project harness --extra dev pytest harness/tests -q`

## Not yet (staged)

- **MCP host.** RAG is wired **in-process** today behind the `RagBackend`
  protocol (`tools/rag_backend.py`); swapping to an MCP stdio child is a config
  change, not a rewrite. Other MCP servers (email, web, calendar) attach here
  later.
- **Gated tools / approval seam.** `gated` (write/act) tools are defined but
  hidden from the model and refused at dispatch until the human-approval path
  lands. Everything wired now is `local` (read-only).
- **Genuine streaming.** Today the final answer is buffered-then-streamed (first
  token waits for the whole tool loop). The voice-phase fix is an immediate
  spoken ack + true streaming of the final turn.
