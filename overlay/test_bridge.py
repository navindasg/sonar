"""Unit tests for the overlay bridge's SSE parser (contract with osvoice)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge import sse_delta  # noqa: E402


def test_extracts_content_delta():
    line = 'data: {"choices":[{"delta":{"content":"hello "}}]}'
    assert sse_delta(line) == "hello "


def test_done_sentinel_is_none():
    assert sse_delta("data: [DONE]") is None


def test_non_data_line_is_none():
    assert sse_delta(": keep-alive comment") is None
    assert sse_delta("event: ping") is None


def test_empty_choices_is_none():
    assert sse_delta('data: {"choices":[]}') is None


def test_delta_without_content_is_none():
    # terminal chunk: delta:{}, finish_reason:stop
    assert sse_delta('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}') is None


def test_malformed_json_is_none():
    assert sse_delta("data: {not json") is None
