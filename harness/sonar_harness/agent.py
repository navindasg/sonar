"""Bounded tool-use loop over local gemma (Ollama).

Ported from brook37 ``daemon/agent.py::_process_todo`` and adapted:
  * target is local Ollama (``/api/chat``), not Anthropic;
  * tool calls are extracted native-first with an XML-heal fallback for the
    flaky small model (``toolcall.extract_tool_calls``);
  * tool-selection iterations run NON-streaming and are bounded (~8, plenty for
    voice); only the final answer is handed back for streaming to TTS;
  * tool SELECTION runs on the fast model, but when a turn actually used tools
    the final grounded SYNTHESIS is escalated to the reasoning model — small
    local models select tools reliably yet synthesize weakly (gated by
    ``models.escalate_synthesis_after_tools``);
  * every step is bracketed with step-events (CONTRACTS.md §3) via ``ctx.emit``;
  * the synchronous per-request turn drops brook37's queue/supervisor/channels.

``run_turn`` returns the final answer text plus a small trace summary. The
server streams that final text as SSE. Tool iterations having already run, the
model's grounded answer is deterministic to stream.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sonar_harness.events import EventSink
from sonar_harness.model_router import ModelsConfig, pick_model
from sonar_harness.ollama_client import OllamaChat, to_ollama_tools
from sonar_harness.prompt import build_system_prompt
from sonar_harness.state import State
from sonar_harness.toolcall import extract_tool_calls
from sonar_harness.tools.base import ToolContext, ToolRegistry

log = logging.getLogger("sonar.agent")

MAX_TOOL_ITERATIONS = 8  # voice turns rarely need more than 1-2 tool calls


@dataclass
class TurnResult:
    turn_id: str
    text: str
    model: str
    iterations: int
    tool_calls: int
    parse_paths: list[str] = field(default_factory=list)


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else ""
    return ""


def _chat_or_fallback(
    ollama: OllamaChat,
    model: str,
    fallback_model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    models: ModelsConfig,
    emit,
) -> tuple[dict[str, Any], str]:
    """Call ``model``; on failure, retry once on the always-on fast model.

    A missing or broken escalation model (e.g. the 12b/26b not pulled, or evicted
    mid-turn) then degrades gracefully to the pinned fast model instead of
    500-ing the whole turn. Returns ``(message, effective_model)`` so the loop
    keeps using whichever model actually answered.
    """
    try:
        message = ollama.chat(
            model, messages, tools, keep_alive=models.keep_alive_for(model)
        )
        return message, model
    except RuntimeError:
        if model == fallback_model:
            raise  # the fast model itself failed — nothing left to fall back to
        log.warning("model %s failed; falling back to %s", model, fallback_model)
        emit({"step": "model_switch", "detail": f"fallback→{fallback_model}"})
        message = ollama.chat(
            fallback_model,
            messages,
            tools,
            keep_alive=models.keep_alive_for(fallback_model),
        )
        return message, fallback_model


def run_turn(
    *,
    inbound_messages: list[dict[str, Any]],
    charter: str,
    registry: ToolRegistry,
    ollama: OllamaChat,
    models: ModelsConfig,
    state: State,
    events: EventSink,
    turn_id: str | None = None,
) -> TurnResult:
    """Run the bounded tool loop for one user turn; return the final answer.

    ``inbound_messages`` is the OpenAI-style messages array from the /v1
    request (system messages there are ignored — the harness owns the charter).
    """
    turn_id = turn_id or uuid.uuid4().hex[:12]
    emit = events.emitter(turn_id)
    ctx = ToolContext(turn_id=turn_id, state=state, emit=emit)

    user_text = _first_user_text(inbound_messages)
    emit({"step": "turn_start", "detail": user_text[:120]})

    model, difficulty_escalated = pick_model(user_text, models)
    if difficulty_escalated:
        emit({"step": "model_switch", "detail": f"difficulty→reason: {model}"})
    reason_model = models.resolve(models.escalation)
    fast_model = models.resolve(models.default)
    log.info("turn=%s model=%s", turn_id, model)

    # Preserve prior conversation (user/assistant/tool) from the request, but
    # replace any client-sent system prompt with the harness charter layer.
    history = [m for m in inbound_messages if m.get("role") != "system"]
    system_prompt = build_system_prompt(charter)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        *history,
    ]

    schemas = registry.schemas_for(ctx)
    tools = to_ollama_tools(schemas)
    tool_names = frozenset(registry.names())

    final_text = ""
    total_tool_calls = 0
    parse_paths: list[str] = []
    iteration = 0

    for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
        # Per-model keep_alive: the pinned fast model stays warm; the on-demand
        # reasoner uses its short idle TTL so it only holds RAM while in use. If
        # the escalation model is unavailable, degrade to fast instead of failing.
        message, model = _chat_or_fallback(
            ollama, model, fast_model, messages, tools, models, emit
        )
        calls, via = extract_tool_calls(message, tool_names)
        parse_paths.append(via)

        if not calls:
            # Small models reliably SELECT tools but fumble grounded SYNTHESIS
            # (verify finding: e4b retrieved the right chunk yet said "I don't
            # have details"). If this turn used tools and we're still on the fast
            # model, discard the fast draft and re-synthesize on the reasoner.
            if (
                models.escalate_synthesis_after_tools
                and total_tool_calls > 0
                and model != reason_model
            ):
                model = reason_model
                emit(
                    {"step": "model_switch", "detail": f"synthesis→reason: {model}"}
                )
                continue
            final_text = (message.get("content") or "").strip()
            break

        # Append the assistant turn (native tool_calls carried through so the
        # model sees its own call paired with the result on the next turn).
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or "",
        }
        if message.get("tool_calls"):
            assistant_msg["tool_calls"] = message["tool_calls"]
        messages.append(assistant_msg)

        for call in calls:
            total_tool_calls += 1
            emit(
                {
                    "step": "tool",
                    "tool": call.name,
                    "detail": _args_summary(call.args),
                    "status": "pending",
                }
            )
            try:
                result = registry.dispatch(call.name, call.args, ctx)
            except (KeyError, PermissionError) as exc:
                result = f"error: {exc}"
                emit(
                    {
                        "step": "tool_result_summary",
                        "tool": call.name,
                        "detail": str(exc),
                        "status": "error",
                    }
                )
            except Exception as exc:  # surface to model, don't kill the turn
                log.exception("tool %r raised (turn=%s)", call.name, turn_id)
                result = f"error: {type(exc).__name__}: {exc}"
                emit(
                    {
                        "step": "tool_result_summary",
                        "tool": call.name,
                        "detail": f"{type(exc).__name__}",
                        "status": "error",
                    }
                )
            messages.append(
                {"role": "tool", "tool_name": call.name, "content": result}
            )

    if not final_text:
        # Ran out of iterations mid-tool-loop, or the model returned empty text.
        final_text = (
            "I looked but couldn't put together a clear answer just now. "
            "Try asking me a little differently."
        )
        log.warning("turn=%s produced no final text (iterations=%d)", turn_id, iteration)

    emit({"step": "final", "detail": "streaming reply"})
    return TurnResult(
        turn_id=turn_id,
        text=final_text,
        model=model,
        iterations=iteration,
        tool_calls=total_tool_calls,
        parse_paths=parse_paths,
    )


def _args_summary(args: dict[str, Any]) -> str:
    """Compact <=120 char rendering of tool args for a step-event detail."""
    parts = []
    for k, v in args.items():
        sval = str(v)
        if len(sval) > 60:
            sval = sval[:59] + "…"
        parts.append(f"{k}={sval}")
    return ", ".join(parts)[:120]
