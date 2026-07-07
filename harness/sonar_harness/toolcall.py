"""Tool-call extraction: native tool_calls primary, XML-heal fallback.

Ported and generalized from the checked-in Stream A spike
(``harness/spikes/gemma_toolcall_spike.py``). The spike proved that
gemma4:e4b-mlx emits well-formed structured ``tool_calls`` via Ollama's
``/api/chat`` on the first turn, but small local models are flaky —
especially on follow-up turns after a tool result — and sometimes emit a
call as *text* instead. So the harness policy (DECISIONS.md) is:

    native JSON tool_calls  (primary)
        -> XML-emitted-as-text, parsed/repaired  (auto-heal fallback)

Unlike the spike, tool names are NOT hard-coded here — they are passed in
from the live registry, so healing works for any registered tool.

Every extractor returns a list of ``ToolCall(name, args)``; an empty list
means "the model produced a final answer, not a tool call."
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("sonar.toolcall")


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


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


def _parse_native(message: dict[str, Any]) -> list[ToolCall]:
    """Extract structured tool_calls (Ollama native / OpenAI shape)."""
    calls = message.get("tool_calls")
    if not calls:
        return []
    out: list[ToolCall] = []
    for call in calls:
        fn = call.get("function", call)
        name = fn.get("name")
        args = _coerce_args(fn.get("arguments"))
        if isinstance(name, str) and name and isinstance(args, dict):
            out.append(ToolCall(name=name, args=args))
    return out


# XML-heal: recover a tool call the model emitted as *text*.
_HEAL_PATTERNS = [
    # gemma often emits ```tool_code\nrag_search(query="...")\n```
    re.compile(r"```(?:tool_code|python)\s*(.+?)```", re.DOTALL),
    # <tool_call>{...}</tool_call> / <function_call>{...}</function_call>
    re.compile(r"<tool_call>\s*(.+?)\s*</tool_call>", re.DOTALL),
    re.compile(r"<function_call>\s*(.+?)\s*</function_call>", re.DOTALL),
    # fenced json blob
    re.compile(r"```(?:json)?\s*(\{.+?\})\s*```", re.DOTALL),
]

_KWARG_RE = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*')")


def _repair_json(blob: str) -> Any | None:
    """Parse a JSON blob, retrying after light repair (single quotes, trailing commas)."""
    for attempt in (blob, _light_repair(blob)):
        try:
            return json.loads(attempt)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _light_repair(blob: str) -> str:
    repaired = re.sub(r",\s*([}\]])", r"\1", blob)  # trailing commas
    # Single -> double quotes only when the blob has no double quotes at all,
    # to avoid mangling apostrophes inside already-valid JSON strings.
    if '"' not in repaired and "'" in repaired:
        repaired = repaired.replace("'", '"')
    return repaired


def _heal_from_text(text: str, tool_names: frozenset[str]) -> list[ToolCall]:
    """Best-effort recovery of tool calls from raw model text."""
    if not text:
        return []
    candidates: list[str] = []
    for pat in _HEAL_PATTERNS:
        candidates.extend(pat.findall(text))
    candidates.append(text)  # last resort: scan the whole thing

    call_re = re.compile(
        r"(" + "|".join(re.escape(n) for n in tool_names) + r")\s*\(\s*(.*?)\s*\)",
        re.DOTALL,
    ) if tool_names else None

    for cand in candidates:
        cand = cand.strip()
        # (a) JSON object with name + arguments/parameters
        obj = _repair_json(cand)
        if isinstance(obj, dict):
            name = obj.get("name")
            args = _coerce_args(obj.get("arguments", obj.get("parameters")))
            if name in tool_names and isinstance(args, dict):
                return [ToolCall(name=name, args=args)]
        # (b) python-ish call: rag_search(query="...")
        if call_re is not None:
            m = call_re.search(cand)
            if m:
                name = m.group(1)
                kwargs = {k: v[1:-1] for k, v in _KWARG_RE.findall(m.group(2))}
                if kwargs:
                    return [ToolCall(name=name, args=kwargs)]
    return []


def extract_tool_calls(
    message: dict[str, Any], tool_names: frozenset[str]
) -> tuple[list[ToolCall], str]:
    """Return ``(tool_calls, via)`` for one assistant message.

    ``via`` is ``"native"`` | ``"xml_heal"`` | ``"none"`` — surfaced so the
    caller can log how flaky the model was this turn. Precedence: structured
    tool_calls win; only if there are none do we heal from the text content.
    """
    native = _parse_native(message)
    if native:
        return native, "native"
    healed = _heal_from_text(message.get("content") or "", tool_names)
    if healed:
        log.info("recovered %d tool call(s) via xml-heal", len(healed))
        return healed, "xml_heal"
    return [], "none"
