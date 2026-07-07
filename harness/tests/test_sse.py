"""The /v1 SSE deltas match what osvoice's OpenAICompatLLM parses.

Mirrors voice/osvoice/providers/llm_openai.py::_iter_deltas / _extract_delta so
a drift in the harness's wire shape fails here rather than in the voice loop.
"""

from __future__ import annotations

import json
import re

from sonar_harness.server import _sse_chunk, _sse_final

_PREFIX = "data: "


def _parse_line(line: str):
    assert line.startswith(_PREFIX)
    data = line[len(_PREFIX):].strip()
    if data == "[DONE]":
        return "[DONE]"
    obj = json.loads(data)
    return obj["choices"][0].get("delta", {}).get("content")


def test_sse_chunk_shape_is_osvoice_parseable():
    raw = _sse_chunk("gemma4:e4b-mlx", "abc123", "hello ")
    assert raw.endswith("\n\n")
    line = raw.split("\n\n")[0]
    assert _parse_line(line) == "hello "


def test_sse_final_emits_stop_then_done():
    raw = _sse_final("gemma4:e4b-mlx", "abc123")
    lines = [l for l in raw.split("\n\n") if l.strip()]
    stop_obj = json.loads(lines[0][len(_PREFIX):])
    assert stop_obj["choices"][0]["finish_reason"] == "stop"
    assert lines[1] == "data: [DONE]"


def test_full_stream_roundtrip_reconstructs_text():
    text = "The WSN pipeline is three-tier."
    tokens = re.compile(r"\S+\s*").findall(text)
    chunks = [_sse_chunk("m", "t", tok) for tok in tokens] + [_sse_final("m", "t")]
    out: list[str] = []
    for raw in chunks:
        for line in [l for l in raw.split("\n\n") if l.strip()]:
            d = _parse_line(line)
            if d not in (None, "[DONE]"):
                out.append(d)
    assert "".join(out) == text
