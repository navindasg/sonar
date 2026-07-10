"""Unit tests for the external-input tools (gmail / calendar / web search).

Covers the pure render/normalize helpers, and the graceful degradation paths
(not-connected / not-configured return a model-safe string, never raise). Live
Google/Tavily calls need real credentials and are out of scope here.
"""
from __future__ import annotations

import pytest

from sonar_harness.tools.base import ToolContext
from sonar_harness.tools.calendar_read import (
    CalendarAgendaTool,
    _event_start,
    render_events,
)
from sonar_harness.tools.calendar_write import CalendarCreateTool, build_event_body
from sonar_harness.tools.gmail_read import GmailSearchTool, _header, render_messages
from sonar_harness.tools.web_search import (
    WebSearchTool,
    _normalize_searxng,
    _normalize_tavily,
    render_results,
)


def _ctx() -> ToolContext:
    return ToolContext(turn_id="t", state=None, emit=lambda _e: None)


# ---- gmail render -------------------------------------------------------------

def _msg(sender: str, subject: str, date: str, snippet: str) -> dict:
    return {
        "snippet": snippet,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": date},
            ]
        },
    }


def test_gmail_header_is_case_insensitive() -> None:
    m = _msg("alice@x.com", "Hi", "Wed", "hello")
    assert _header(m, "from") == "alice@x.com"
    assert _header(m, "SUBJECT") == "Hi"
    assert _header(m, "missing") == ""


def test_gmail_render_messages() -> None:
    assert render_messages([]) == "No matching messages."
    out = render_messages([_msg("alice@x.com", "Invoice", "Wed", "your invoice is ready")])
    assert "Invoice" in out and "alice@x.com" in out and "invoice is ready" in out


def test_gmail_not_connected_returns_string(monkeypatch) -> None:
    # Point the token at a path that cannot exist -> "not connected" (or, if the
    # google libs are absent, "not installed"): either way a string, not a raise.
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/sonar/google_token.json")
    result = GmailSearchTool().run({"query": "is:unread"}, _ctx())
    assert isinstance(result, str) and "google" in result.lower()


# ---- calendar render ----------------------------------------------------------

def test_calendar_event_start_prefers_datetime() -> None:
    assert _event_start({"start": {"dateTime": "2026-07-09T10:00:00Z"}}) == "2026-07-09T10:00:00Z"
    assert _event_start({"start": {"date": "2026-07-09"}}) == "2026-07-09"
    assert _event_start({"start": {}}) == ""


def test_calendar_render_events() -> None:
    assert render_events([]) == "No events in that window."
    out = render_events(
        [{"start": {"dateTime": "2026-07-09T10:00:00Z"}, "summary": "Standup", "location": "Zoom"}]
    )
    assert "Standup" in out and "Zoom" in out


def test_calendar_not_connected_returns_string(monkeypatch) -> None:
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/sonar/google_token.json")
    result = CalendarAgendaTool().run({"days": 1}, _ctx())
    assert isinstance(result, str) and "google" in result.lower()


# ---- calendar write (event body builder) --------------------------------------

def test_build_event_body_defaults_end_to_one_hour() -> None:
    body = build_event_body({"summary": "Dentist", "start": "2026-07-10T15:00:00-04:00"})
    assert body["summary"] == "Dentist"
    assert body["start"]["dateTime"] == "2026-07-10T15:00:00-04:00"
    assert body["end"]["dateTime"] == "2026-07-10T16:00:00-04:00"


def test_build_event_body_honors_duration_and_optionals() -> None:
    body = build_event_body(
        {
            "summary": "Sync",
            "start": "2026-07-10T09:00:00-04:00",
            "duration_minutes": 30,
            "location": "Zoom",
            "description": "weekly",
        }
    )
    assert body["end"]["dateTime"] == "2026-07-10T09:30:00-04:00"
    assert body["location"] == "Zoom" and body["description"] == "weekly"


def test_build_event_body_explicit_end_wins() -> None:
    body = build_event_body(
        {"summary": "Block", "start": "2026-07-10T09:00:00-04:00", "end": "2026-07-10T11:30:00-04:00"}
    )
    assert body["end"]["dateTime"] == "2026-07-10T11:30:00-04:00"


def test_build_event_body_naive_start_gets_local_offset() -> None:
    body = build_event_body({"summary": "X", "start": "2026-07-10T15:00:00"})
    # A naive start is made local-aware, so the serialized value carries an offset.
    assert "+" in body["start"]["dateTime"] or "-" in body["start"]["dateTime"][11:]


def test_build_event_body_rejects_missing_fields() -> None:
    with pytest.raises(ValueError):
        build_event_body({"start": "2026-07-10T15:00:00"})  # no summary
    with pytest.raises(ValueError):
        build_event_body({"summary": "x"})  # no start
    with pytest.raises(ValueError):
        build_event_body({"summary": "x", "start": "not-a-date"})


def test_calendar_create_not_connected_returns_string(monkeypatch) -> None:
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", "/nonexistent/sonar/google_token.json")
    result = CalendarCreateTool().run(
        {"summary": "Test", "start": "2026-07-10T15:00:00"}, _ctx()
    )
    assert isinstance(result, str) and "google" in result.lower()


# ---- web search ---------------------------------------------------------------

def test_normalize_tavily_and_searxng() -> None:
    tav = {"results": [{"title": "T", "url": "u", "content": "c"}, "junk", {"title": "T2", "url": "u2", "content": "c2"}]}
    assert _normalize_tavily(tav) == [
        {"title": "T", "url": "u", "snippet": "c"},
        {"title": "T2", "url": "u2", "snippet": "c2"},
    ]
    sx = {"results": [{"title": "S", "url": "su", "content": "sc"}]}
    assert _normalize_searxng(sx) == [{"title": "S", "url": "su", "snippet": "sc"}]
    assert _normalize_tavily({}) == []


def test_web_render_results() -> None:
    assert render_results([]) == "No web results found."
    out = render_results([{"title": "Docs", "url": "https://x", "snippet": "how to"}])
    assert "Docs" in out and "https://x" in out


def test_web_search_requires_query() -> None:
    assert "query" in WebSearchTool().run({}, _ctx()).lower()


def test_web_search_tavily_missing_key_returns_string(monkeypatch) -> None:
    monkeypatch.setenv("SONAR_SEARCH_PROVIDER", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = WebSearchTool().run({"query": "weather"}, _ctx())
    assert "TAVILY_API_KEY" in result


def test_web_search_unknown_provider_returns_string(monkeypatch) -> None:
    monkeypatch.setenv("SONAR_SEARCH_PROVIDER", "bing")
    result = WebSearchTool().run({"query": "weather"}, _ctx())
    assert "unknown" in result.lower()
