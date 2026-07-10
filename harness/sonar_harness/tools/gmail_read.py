"""gmail.search — read-only search over the user's OWN Gmail.

Uses the per-user OAuth credentials from ``google_auth`` (read-only scope). Never
sends or drafts: those are a human-gated capability for a later pass (DECISIONS:
email is draft-only, never auto-sent). Not connected yet → returns a clear
"run google-auth" string the model can relay, rather than crashing the turn.
"""
from __future__ import annotations

from typing import Any

from sonar_harness.google_auth import GoogleAuthError, build_service
from sonar_harness.tools.base import ToolBase, ToolContext

_DEFAULT_MAX = 10
_MAX_RESULTS = 25
_SNIPPET = 200


def _header(msg: dict[str, Any], name: str) -> str:
    """Pull a header value (From/Subject/Date) from a metadata-format message."""
    headers = (msg.get("payload") or {}).get("headers") or []
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return str(h.get("value", "")).strip()
    return ""


def render_messages(messages: list[dict[str, Any]]) -> str:
    """Render metadata-format Gmail messages into compact model-readable lines."""
    if not messages:
        return "No matching messages."
    lines: list[str] = []
    for i, m in enumerate(messages, 1):
        subject = _header(m, "Subject") or "(no subject)"
        sender = _header(m, "From")
        date = _header(m, "Date")
        snippet = str(m.get("snippet", "")).strip().replace("\n", " ")
        if len(snippet) > _SNIPPET:
            snippet = snippet[: _SNIPPET - 1] + "…"
        lines.append(f"[{i}] {subject} — {sender} ({date})\n    {snippet}")
    return "\n".join(lines)


class GmailSearchTool(ToolBase):
    name = "gmail.search"
    description = (
        "Search the user's OWN Gmail (read-only) and return matching messages: "
        "sender, subject, date, and a snippet. Use Gmail search operators in "
        "'query' — e.g. 'is:unread', 'from:alice newer_than:2d', "
        "'subject:invoice', 'in:inbox is:important'. Leave query empty for the "
        "most recent inbox mail. Cannot send or draft email."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Gmail search query (operators supported); empty = recent inbox.",
            },
            "max_results": {
                "type": "integer",
                "description": f"How many messages to return (1-{_MAX_RESULTS}, default {_DEFAULT_MAX}).",
            },
        },
        "required": [],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = args.get("query")
        query = query.strip() if isinstance(query, str) else ""
        raw_max = args.get("max_results", _DEFAULT_MAX)
        try:
            max_results = max(1, min(_MAX_RESULTS, int(raw_max)))
        except (TypeError, ValueError):
            max_results = _DEFAULT_MAX

        try:
            service = build_service("gmail", "v1")
        except GoogleAuthError as exc:
            ctx.emit(_summary("gmail.search", str(exc), status="error"))
            return str(exc)

        try:
            listing = (
                service.users()
                .messages()
                .list(userId="me", q=query or None, maxResults=max_results)
                .execute()
            )
            ids = [m["id"] for m in listing.get("messages", []) if "id" in m]
            messages = [
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
                for mid in ids
            ]
        except Exception as exc:  # noqa: BLE001 — map API failure to model text
            ctx.emit(_summary("gmail.search", type(exc).__name__, status="error"))
            return f"error: Gmail request failed ({type(exc).__name__}): {exc}"

        ctx.emit(_summary("gmail.search", f"{len(messages)} messages"))
        return render_messages(messages)


def _summary(tool: str, detail: str, *, status: str = "ok") -> dict[str, Any]:
    return {"step": "tool_result_summary", "tool": tool, "detail": detail, "status": status}
