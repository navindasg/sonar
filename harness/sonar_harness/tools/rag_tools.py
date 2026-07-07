"""The two RAG tools wired for this pass: rag.search and rag.note_context.

Both are thin ``ToolBase`` wrappers over a ``RagBackend`` (in-process today,
MCP stdio child tomorrow — a config change, per rag_backend.py). Each renders
the backend's ``dict`` result into a compact text block the model reads, and
emits a ``tool_result_summary`` step-event so the overlay can show what came
back. The backend never raises; a ``{"error": ...}`` dict is surfaced to the
model as text (with its ``suggestion``) rather than crashing the turn.
"""

from __future__ import annotations

import json
from typing import Any

from sonar_harness.tools.base import ToolBase, ToolContext
from sonar_harness.tools.rag_backend import RagBackend

_MAX_SNIPPET = 400
_MAX_RESULTS = 5


def _summary_event(tool: str, detail: str, *, status: str = "ok") -> dict[str, Any]:
    return {"step": "tool_result_summary", "tool": tool, "detail": detail, "status": status}


class RagSearchTool(ToolBase):
    name = "rag.search"
    description = (
        "Semantic search over the user's personal Obsidian notes. Use this to "
        "answer anything that might live in their own writing — projects, "
        "decisions, people, prior notes. Returns the most relevant passages."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language search query.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: only passages carrying at least one of these tags.",
            },
        },
        "required": ["query"],
    }
    permission = "local"

    def __init__(self, backend: RagBackend) -> None:
        self._backend = backend

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return "error: rag.search requires a non-empty 'query' string."
        tags = args.get("tags") if isinstance(args.get("tags"), list) else None
        result = self._backend.search(query.strip(), tags=tags)

        if "error" in result:
            ctx.emit(_summary_event("rag.search", result["error"], status="error"))
            return f"error: {result['error']} ({result.get('suggestion', '')})"

        hits = result.get("results", [])[:_MAX_RESULTS]
        if not hits:
            msg = result.get("message", "no matching notes found")
            ctx.emit(_summary_event("rag.search", "0 passages", status="ok"))
            return f"No matching notes. {msg}"

        lines: list[str] = []
        for i, h in enumerate(hits, 1):
            snippet = str(h.get("snippet", "")).strip().replace("\n", " ")
            if len(snippet) > _MAX_SNIPPET:
                snippet = snippet[: _MAX_SNIPPET - 1] + "…"
            lines.append(
                f"[{i}] {h.get('source_path', '?')}"
                f" ({h.get('heading_path', '')}, score {h.get('relevance_score', 0)}): {snippet}"
            )
        ctx.emit(_summary_event("rag.search", f"{len(hits)} passages matched"))
        return "\n".join(lines)


class RagNoteContextTool(ToolBase):
    name = "rag.note_context"
    description = (
        "Given a note's vault-relative path, return the note plus its wikilink "
        "neighbours: the notes it links to (forward_links) and the notes that "
        "link to it (backlinks). Use after rag.search when you want a note's "
        "connections in the user's knowledge graph."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Vault-relative path to the note, e.g. 'projects/wsn-pipeline.md'.",
            }
        },
        "required": ["path"],
    }
    permission = "local"

    def __init__(self, backend: RagBackend) -> None:
        self._backend = backend

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return "error: rag.note_context requires a non-empty 'path' string."
        result = self._backend.note_context(path.strip())

        if "error" in result:
            ctx.emit(_summary_event("rag.note_context", result["error"], status="error"))
            return f"error: {result['error']} ({result.get('suggestion', '')})"

        note = result.get("note", {})
        content = str(note.get("content", "")).strip()
        if len(content) > _MAX_SNIPPET * 2:
            content = content[: _MAX_SNIPPET * 2 - 1] + "…"
        forward = [fl.get("path") for fl in result.get("forward_links", [])]
        back = [bl.get("path") if isinstance(bl, dict) else bl for bl in result.get("backlinks", [])]
        ctx.emit(
            _summary_event(
                "rag.note_context",
                f"{len(forward)} forward, {len(back)} backlinks",
            )
        )
        return json.dumps(
            {
                "path": note.get("path", path),
                "content": content,
                "forward_links": forward,
                "backlinks": back,
            },
            ensure_ascii=False,
        )
