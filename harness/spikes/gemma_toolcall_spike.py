# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Stream A spike: can local gemma emit well-formed tool calls?

Sends a set of tool-triggering prompts to BOTH gemma models (e4b fast,
26b reasoning) over TWO transports:

  1. native  -> Ollama /api/chat with `tools`, read message.tool_calls
  2. openai  -> Ollama /v1/chat/completions (OpenAI-compat) with `tools`,
                read choices[0].message.tool_calls

For every response we score three parse-paths, in this precedence order:

  native_tool_calls  -> a structured tool_calls field came back
  xml_heal           -> no structured call, but the *text* content embeds a
                        recoverable call (```tool_code / ```json / <tool_call>
                        / bare {"name":...,"arguments":...}) we can parse+repair

A call is "well-formed" iff: names a REAL tool (get_weather|rag_search) AND
its arguments parse as a JSON object AND carry the tool's required key.
"correct" additionally requires the tool matches the prompt's intended tool.

Stdlib only (urllib) so it is `uv run` / `python3` runnable with no install.
Prints a human summary AND a machine-readable JSON blob (--json) that the
orchestrator/harness config can consume.

Usage:
  uv run harness/spikes/gemma_toolcall_spike.py
  uv run harness/spikes/gemma_toolcall_spike.py --json
  uv run harness/spikes/gemma_toolcall_spike.py --models gemma4:e4b-mlx
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

OLLAMA_HOST = "http://127.0.0.1:11434"
MODELS = ["gemma4:e4b-mlx", "gemma4:26b-mlx"]
REQUEST_TIMEOUT = 180  # seconds; 26b cold-load can be slow

# ---------------------------------------------------------------------------
# Tool schema (2 tools) — OpenAI/JSON-Schema shape, reused for both transports.
# ---------------------------------------------------------------------------
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City name, e.g. 'Paris' or 'New York'.",
                    }
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": (
                "Search the user's personal Obsidian notes / knowledge base "
                "for relevant passages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query.",
                    }
                },
                "required": ["query"],
            },
        },
    },
]

REAL_TOOLS = {"get_weather": "city", "rag_search": "query"}

SYSTEM_PROMPT = (
    "You are a voice assistant with tools. When the user's request needs live "
    "weather or something from their personal notes, CALL THE APPROPRIATE TOOL "
    "rather than answering from memory. Prefer a tool call over prose."
)

# ~10 varied prompts that SHOULD trigger a tool. `expect` = intended tool.
PROMPTS: list[dict[str, str]] = [
    {"expect": "get_weather", "text": "What's the weather in Tokyo right now?"},
    {"expect": "get_weather", "text": "Do I need an umbrella in London today?"},
    {"expect": "get_weather", "text": "How hot is it going to be in Phoenix?"},
    {"expect": "get_weather", "text": "Tell me the current conditions in Reykjavik."},
    {"expect": "get_weather", "text": "Is it cold out in Chicago at the moment?"},
    {"expect": "rag_search", "text": "What did I write in my notes about the Sonar architecture?"},
    {"expect": "rag_search", "text": "Find my notes on the harness tool loop."},
    {"expect": "rag_search", "text": "Search my knowledge base for the model router decision."},
    {"expect": "rag_search", "text": "Look up what I saved about launchd scheduling."},
    {"expect": "rag_search", "text": "Pull up my note about the Obsidian vault path."},
]

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _post(path: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, float]:
    """POST JSON, return (parsed_json, error, elapsed_seconds)."""
    url = f"{OLLAMA_HOST}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
        elapsed = time.perf_counter() - start
        return json.loads(body), None, elapsed
    except urllib.error.HTTPError as exc:
        elapsed = time.perf_counter() - start
        detail = exc.read().decode("utf-8", "replace")[:300]
        return None, f"HTTP {exc.code}: {detail}", elapsed
    except Exception as exc:  # noqa: BLE001 - spike; report any failure
        elapsed = time.perf_counter() - start
        return None, f"{type(exc).__name__}: {exc}", elapsed


# ---------------------------------------------------------------------------
# Parse paths
# ---------------------------------------------------------------------------


@dataclass
class ParseOutcome:
    path: str  # native_tool_calls | xml_heal | none
    tool_name: str | None = None
    args: dict[str, Any] | None = None
    well_formed: bool = False  # real tool + valid JSON-object args + required key
    correct: bool = False  # well_formed AND matches expected tool
    raw_snippet: str = ""


def _validate(tool_name: str | None, args: Any, expect: str) -> tuple[bool, bool]:
    """(well_formed, correct)."""
    if tool_name not in REAL_TOOLS:
        return False, False
    if not isinstance(args, dict):
        return False, False
    required = REAL_TOOLS[tool_name]
    val = args.get(required)
    well_formed = isinstance(val, str) and bool(val.strip())
    correct = well_formed and tool_name == expect
    return well_formed, correct


def _coerce_args(raw: Any) -> dict[str, Any] | None:
    """tool_calls arguments may be a dict or a JSON string; normalize to dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def parse_native(message: dict[str, Any], expect: str) -> ParseOutcome | None:
    """Extract a structured tool_calls entry (Ollama native or OpenAI shape)."""
    calls = message.get("tool_calls")
    if not calls:
        return None
    call = calls[0]
    fn = call.get("function", call)
    name = fn.get("name")
    args = _coerce_args(fn.get("arguments"))
    well, correct = _validate(name, args, expect)
    return ParseOutcome(
        path="native_tool_calls",
        tool_name=name,
        args=args if isinstance(args, dict) else None,
        well_formed=well,
        correct=correct,
        raw_snippet=json.dumps(call)[:200],
    )


# XML-heal: recover a tool call emitted as *text* by a flaky small model.
_HEAL_PATTERNS = [
    # Gemma frequently emits ```tool_code\nget_weather(city="Tokyo")\n```
    re.compile(r"```(?:tool_code|python)\s*(.+?)```", re.DOTALL),
    # <tool_call>{...}</tool_call> or <function_call>{...}</function_call>
    re.compile(r"<tool_call>\s*(.+?)\s*</tool_call>", re.DOTALL),
    re.compile(r"<function_call>\s*(.+?)\s*</function_call>", re.DOTALL),
    # fenced json blob
    re.compile(r"```(?:json)?\s*(\{.+?\})\s*```", re.DOTALL),
]

_CALL_RE = re.compile(r"(get_weather|rag_search)\s*\(\s*(.*?)\s*\)", re.DOTALL)
_KWARG_RE = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*')")


def _heal_from_text(text: str) -> tuple[str | None, dict[str, Any] | None]:
    """Best-effort recovery of (tool_name, args) from raw model text."""
    if not text:
        return None, None
    candidates: list[str] = []
    for pat in _HEAL_PATTERNS:
        candidates.extend(pat.findall(text))
    candidates.append(text)  # last resort: scan the whole thing

    for cand in candidates:
        cand = cand.strip()
        # (a) JSON object with name/arguments (or name/parameters)
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                name = obj.get("name")
                args = _coerce_args(obj.get("arguments", obj.get("parameters")))
                if name in REAL_TOOLS and isinstance(args, dict):
                    return name, args
        except (json.JSONDecodeError, TypeError):
            pass
        # (b) python-ish call: get_weather(city="Tokyo")
        m = _CALL_RE.search(cand)
        if m:
            name = m.group(1)
            kwargs = {k: v[1:-1] for k, v in _KWARG_RE.findall(m.group(2))}
            if kwargs:
                return name, kwargs
            # positional single arg: get_weather("Tokyo")
            pos = re.search(r"\"([^\"]*)\"|'([^']*)'", m.group(2))
            if pos and name in REAL_TOOLS:
                return name, {REAL_TOOLS[name]: pos.group(1) or pos.group(2)}
    return None, None


def parse_xml_heal(message: dict[str, Any], expect: str) -> ParseOutcome:
    text = message.get("content") or ""
    name, args = _heal_from_text(text)
    if name is None:
        return ParseOutcome(path="none", raw_snippet=text[:200])
    well, correct = _validate(name, args, expect)
    return ParseOutcome(
        path="xml_heal",
        tool_name=name,
        args=args if isinstance(args, dict) else None,
        well_formed=well,
        correct=correct,
        raw_snippet=text[:200],
    )


def extract_message(api: str, resp: dict[str, Any]) -> dict[str, Any]:
    """Normalize the assistant message across native and openai shapes."""
    if api == "native":
        return resp.get("message", {}) or {}
    choices = resp.get("choices") or [{}]
    return choices[0].get("message", {}) or {}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    model: str
    api: str
    prompts: int = 0
    errors: int = 0
    native_ok: int = 0  # well-formed via structured tool_calls
    heal_ok: int = 0  # well-formed ONLY after xml-heal (native produced none)
    combined_ok: int = 0  # well-formed via either path
    correct: int = 0  # combined_ok AND matched expected tool
    latencies: list[float] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def rate(self) -> float:
        return self.combined_ok / self.prompts if self.prompts else 0.0

    def native_rate(self) -> float:
        return self.native_ok / self.prompts if self.prompts else 0.0

    def median_latency(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def run_cell(model: str, api: str) -> Cell:
    cell = Cell(model=model, api=api)
    path = "/api/chat" if api == "native" else "/v1/chat/completions"
    for p in PROMPTS:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": p["text"]},
            ],
            "tools": TOOLS,
            "stream": False,
        }
        if api == "native":
            payload["options"] = {"temperature": 0}
        else:
            payload["temperature"] = 0
        resp, err, elapsed = _post(path, payload)
        cell.prompts += 1
        cell.latencies.append(elapsed)
        if err is not None or resp is None:
            cell.errors += 1
            cell.details.append({"prompt": p["text"], "error": err, "elapsed": round(elapsed, 2)})
            continue

        msg = extract_message(api, resp)
        native = parse_native(msg, p["expect"])
        heal = parse_xml_heal(msg, p["expect"])

        chosen = native if (native and native.well_formed) else None
        via_native = bool(chosen)
        if not via_native and heal.well_formed:
            chosen = heal
        if native and not native.well_formed and (chosen is None):
            # native produced a (malformed) structured call — keep for the record
            chosen = native

        outcome = chosen or heal or ParseOutcome(path="none")

        if via_native:
            cell.native_ok += 1
        if outcome.well_formed:
            cell.combined_ok += 1
            if not via_native:
                cell.heal_ok += 1
        if outcome.correct:
            cell.correct += 1

        cell.details.append(
            {
                "prompt": p["text"],
                "expect": p["expect"],
                "path": outcome.path,
                "tool": outcome.tool_name,
                "args": outcome.args,
                "well_formed": outcome.well_formed,
                "correct": outcome.correct,
                "elapsed": round(elapsed, 2),
                "snippet": outcome.raw_snippet if not outcome.well_formed else "",
            }
        )
    return cell


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=MODELS)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    cells: list[Cell] = []
    for model in args.models:
        for api in ("native", "openai"):
            print(f"# running {model} via {api} ...", flush=True)
            cells.append(run_cell(model, api))

    # Human summary
    print("\n=== TOOL-CALL RELIABILITY (well-formed = real tool + valid JSON args) ===")
    header = f"{'model':<16} {'api':<8} {'N':>3} {'native':>7} {'combined':>9} {'correct':>8} {'heal+':>6} {'err':>4} {'p50 s':>7}"
    print(header)
    print("-" * len(header))
    for c in cells:
        print(
            f"{c.model:<16} {c.api:<8} {c.prompts:>3} "
            f"{c.native_rate()*100:>6.0f}% {c.rate()*100:>8.0f}% "
            f"{c.correct/c.prompts*100 if c.prompts else 0:>7.0f}% "
            f"{c.heal_ok:>6} {c.errors:>4} {c.median_latency():>7.2f}"
        )

    if args.json:
        blob = {
            "host": OLLAMA_HOST,
            "cells": [
                {
                    "model": c.model,
                    "api": c.api,
                    "prompts": c.prompts,
                    "errors": c.errors,
                    "native_ok": c.native_ok,
                    "heal_only_ok": c.heal_ok,
                    "combined_ok": c.combined_ok,
                    "correct": c.correct,
                    "native_rate": round(c.native_rate(), 3),
                    "combined_rate": round(c.rate(), 3),
                    "correct_rate": round(c.correct / c.prompts, 3) if c.prompts else 0,
                    "median_latency_s": round(c.median_latency(), 3),
                    "details": c.details,
                }
                for c in cells
            ],
        }
        print("\n=== JSON ===")
        print(json.dumps(blob, indent=2))


if __name__ == "__main__":
    main()
