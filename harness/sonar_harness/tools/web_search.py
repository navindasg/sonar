"""web.search — the one capability that leaves the machine (DECISIONS).

Vendor-agnostic behind a tiny adapter seam so the key/host is a config choice,
not a code change (per the 2026 research: Brave dropped its free tier, Google
CSE closed to new signups, Bing's API is retired):

  * ``tavily``  (default) — LLM-oriented search, 1,000 free/mo, no card.
                Set ``TAVILY_API_KEY``.
  * ``searxng`` — self-hosted metasearch: free, unlimited, fully private
                (no vendor key). Set ``SONAR_SEARXNG_URL`` (e.g. http://127.0.0.1:8888)
                and enable JSON output in the SearXNG settings (formats: [html, json]).

Choose via ``SONAR_SEARCH_PROVIDER`` (default ``tavily``). Not configured →
returns a clear setup string rather than crashing the turn.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from sonar_harness.tools.base import ToolBase, ToolContext

_DEFAULT_MAX = 5
_MAX_RESULTS = 10
_SNIPPET = 300
_TIMEOUT = 15.0
_TAVILY_URL = "https://api.tavily.com/search"


def _normalize_tavily(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Tavily response -> [{title, url, snippet}]."""
    out: list[dict[str, str]] = []
    for r in payload.get("results", []) or []:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "title": str(r.get("title", "")).strip(),
                "url": str(r.get("url", "")).strip(),
                "snippet": str(r.get("content", "")).strip(),
            }
        )
    return out


def _normalize_searxng(payload: dict[str, Any]) -> list[dict[str, str]]:
    """SearXNG JSON response -> [{title, url, snippet}]."""
    out: list[dict[str, str]] = []
    for r in payload.get("results", []) or []:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "title": str(r.get("title", "")).strip(),
                "url": str(r.get("url", "")).strip(),
                "snippet": str(r.get("content", "")).strip(),
            }
        )
    return out


def render_results(results: list[dict[str, str]]) -> str:
    """Render normalized results into compact model-readable lines."""
    if not results:
        return "No web results found."
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        snippet = r.get("snippet", "").replace("\n", " ")
        if len(snippet) > _SNIPPET:
            snippet = snippet[: _SNIPPET - 1] + "…"
        lines.append(f"[{i}] {r.get('title', '(untitled)')} — {r.get('url', '')}\n    {snippet}")
    return "\n".join(lines)


class WebSearchTool(ToolBase):
    name = "web.search"
    description = (
        "Search the public web for CURRENT information that is not in the user's "
        "own notes — news, docs, facts, prices, anything time-sensitive or "
        "external. Returns ranked title/url/snippet results. This is the only "
        "tool that sends data off the machine, so use it only when the answer "
        "isn't in their notes."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search the web for."},
            "max_results": {
                "type": "integer",
                "description": f"How many results (1-{_MAX_RESULTS}, default {_DEFAULT_MAX}).",
            },
        },
        "required": ["query"],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return "error: web.search requires a non-empty 'query'."
        query = query.strip()
        try:
            max_results = max(1, min(_MAX_RESULTS, int(args.get("max_results", _DEFAULT_MAX))))
        except (TypeError, ValueError):
            max_results = _DEFAULT_MAX

        provider = os.environ.get("SONAR_SEARCH_PROVIDER", "tavily").strip().lower()
        try:
            if provider == "tavily":
                results = self._tavily(query, max_results)
            elif provider == "searxng":
                results = self._searxng(query, max_results)
            else:
                return f"error: unknown SONAR_SEARCH_PROVIDER {provider!r} (use 'tavily' or 'searxng')."
        except _NotConfigured as exc:
            ctx.emit(_summary("web.search", "not configured", status="error"))
            return str(exc)
        except Exception as exc:  # noqa: BLE001 — map network/API failure to text
            ctx.emit(_summary("web.search", type(exc).__name__, status="error"))
            return f"error: web search failed ({type(exc).__name__}): {exc}"

        ctx.emit(_summary("web.search", f"{len(results)} results"))
        return render_results(results)

    def _tavily(self, query: str, max_results: int) -> list[dict[str, str]]:
        key = os.environ.get("TAVILY_API_KEY")
        if not key:
            raise _NotConfigured(
                "web.search: Tavily is selected but TAVILY_API_KEY is not set. "
                "Get a free key at tavily.com, or set SONAR_SEARCH_PROVIDER=searxng."
            )
        resp = httpx.post(
            _TAVILY_URL,
            json={"api_key": key, "query": query, "max_results": max_results},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return _normalize_tavily(resp.json())[:max_results]

    def _searxng(self, query: str, max_results: int) -> list[dict[str, str]]:
        base = os.environ.get("SONAR_SEARXNG_URL")
        if not base:
            raise _NotConfigured(
                "web.search: searxng is selected but SONAR_SEARXNG_URL is not set "
                "(e.g. http://127.0.0.1:8888, JSON output enabled)."
            )
        resp = httpx.get(
            base.rstrip("/") + "/search",
            params={"q": query, "format": "json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return _normalize_searxng(resp.json())[:max_results]


class _NotConfigured(RuntimeError):
    """Provider selected but its key/host is missing — model-safe message."""


def _summary(tool: str, detail: str, *, status: str = "ok") -> dict[str, Any]:
    return {"step": "tool_result_summary", "tool": tool, "detail": detail, "status": status}
