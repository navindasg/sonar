"""Unit tests for the pure harness-stream parsers (harness_client)."""
from __future__ import annotations

import json

from harness_client import sse_delta, steps_from_payload


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj)


def test_sse_delta_extracts_content() -> None:
    line = _sse({"choices": [{"delta": {"content": "Caddy"}}]})
    assert sse_delta(line) == "Caddy"


def test_sse_delta_ignores_non_data_and_done() -> None:
    assert sse_delta(": keep-alive") is None
    assert sse_delta("event: ping") is None
    assert sse_delta("data: [DONE]") is None
    assert sse_delta("data:   ") is None


def test_sse_delta_survives_malformed_and_empty_chunks() -> None:
    assert sse_delta("data: {not json") is None
    assert sse_delta(_sse({"choices": []})) is None          # no choices
    assert sse_delta(_sse({"choices": [{"delta": {}}]})) is None  # no content
    assert sse_delta(_sse({})) is None                        # no choices key


def test_steps_from_payload_returns_dict_events_only() -> None:
    payload = {"events": [{"step": "final"}, {"tool": "rag.search"}, "junk", 3, None]}
    assert steps_from_payload(payload) == [{"step": "final"}, {"tool": "rag.search"}]


def test_steps_from_payload_defensive_on_bad_shapes() -> None:
    assert steps_from_payload({}) == []
    assert steps_from_payload({"events": "nope"}) == []
    assert steps_from_payload({"events": None}) == []
    assert steps_from_payload("not a dict") == []
    assert steps_from_payload(None) == []
