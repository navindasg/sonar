"""OpenAI-compatible /v1 server — the STT<->TTS seam.

Exposes ``POST /v1/chat/completions`` with SSE streaming shaped EXACTLY as
``voice/osvoice/providers/llm_openai.py`` parses it:

    data: {"choices":[{"delta":{"content":"..."}}]}
    ...
    data: [DONE]

The tool loop runs NON-streaming (``agent.run_turn``); the grounded final
answer is then emitted as real SSE deltas (buffered-then-streamed — see the
design note in the task return). Step-events for the turn are exposed at
``GET /events`` so the overlay can render the "steps taken" timeline; those
events are also persisted (``event_store``) so ``/events`` can serve history
from before this process started — ``since``/``turn_id``/``limit`` filter it.

Everything shared (registry, RAG backend, Ollama client, model config, state,
event sink, charter) is built once at startup and held on ``app.state``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from sonar_harness.agent import run_turn
from sonar_harness.event_store import DEFAULT_LIMIT, EventStore
from sonar_harness.events import EventSink
from sonar_harness.model_router import load_config as load_models_config
from sonar_harness.nudges import NudgeEngine, empty_snapshot
from sonar_harness.ollama_client import DEFAULT_OLLAMA_URL, OllamaChat
from sonar_harness.prompt import load_charter
from sonar_harness.scheduler import start_scheduler
from sonar_harness.state import State
from sonar_harness.tools import ToolRegistry, default_tools
from sonar_harness.tools.rag_backend import InProcessRagBackend

log = logging.getLogger("sonar.server")

HARNESS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = HARNESS_ROOT.parent
CONFIG_DIR = HARNESS_ROOT / "config"

# Spike default: the checked-in sample vault. Stream B owns the real vault.
DEFAULT_VAULT = REPO_ROOT / "rag" / "tests" / "fixtures" / "sample_vault"

_TOKEN_RE = re.compile(r"\S+\s*")


def _build_state(app: FastAPI) -> None:
    ollama_url = os.environ.get("SONAR_OLLAMA_URL", DEFAULT_OLLAMA_URL)
    vault_path = os.environ.get("SONAR_VAULT_PATH", str(DEFAULT_VAULT))
    embed_model = os.environ.get("SONAR_EMBED_MODEL", "nomic-embed-text")

    log.info("building RAG backend over vault %s", vault_path)
    backend = InProcessRagBackend.build(
        vault_path=vault_path,
        vault_name=os.environ.get("SONAR_VAULT_NAME", "sonar"),
        ollama_url=ollama_url,
        embedding_model=embed_model,
    )
    registry = ToolRegistry.load(
        tools=default_tools(rag_backend=backend, vault_path=vault_path),
        config_path=CONFIG_DIR / "tool_permissions.yaml",
    )
    app.state.registry = registry
    app.state.backend = backend
    app.state.ollama = OllamaChat(base_url=ollama_url)
    app.state.models = load_models_config(CONFIG_DIR / "models.yaml")
    app.state.state = State.open()
    # try_open, not open: a DB we can't write costs history, not the harness —
    # the sink then runs ring-only exactly as it did before.
    app.state.events = EventSink(store=EventStore.try_open())
    app.state.charter = load_charter(CONFIG_DIR / "charter.md")

    # Preload the hot model so the first turn is warm (~1 s) instead of a
    # multi-second cold reload. keep_alive pins it resident thereafter.
    default_model = app.state.models.resolve(app.state.models.default)
    app.state.ollama.warm(default_model)

    log.info(
        "harness ready: %d tools, %d indexed chunks",
        len(registry.names()),
        backend.chunk_count,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _build_state(app)
    app.state.scheduler = start_scheduler(
        vault_path=os.environ.get("SONAR_VAULT_PATH", str(DEFAULT_VAULT))
    )
    try:
        yield
    finally:
        if app.state.scheduler is not None:
            app.state.scheduler.stop()
        app.state.ollama.close()
        app.state.state.close()
        app.state.events.close()


app = FastAPI(title="sonar-harness", lifespan=lifespan)


def _sse_chunk(model: str, turn_id: str, content: str) -> str:
    payload = {
        "id": f"chatcmpl-{turn_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _sse_final(model: str, turn_id: str) -> str:
    payload = {
        "id": f"chatcmpl-{turn_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return JSONResponse(
            {"error": "messages must be a non-empty array"}, status_code=400
        )

    st = request.app.state
    # Run the (blocking) tool loop off the event loop so slow model calls don't
    # stall the server.
    result = await anyio.to_thread.run_sync(
        lambda: run_turn(
            inbound_messages=messages,
            charter=st.charter,
            registry=st.registry,
            ollama=st.ollama,
            models=st.models,
            state=st.state,
            events=st.events,
        )
    )

    stream = bool(body.get("stream", True))
    if not stream:
        # Non-streaming convenience (not used by osvoice, handy for debugging).
        return JSONResponse(
            {
                "id": f"chatcmpl-{result.turn_id}",
                "object": "chat.completion",
                "model": result.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.text},
                        "finish_reason": "stop",
                    }
                ],
                "x_sonar": {
                    "turn_id": result.turn_id,
                    "iterations": result.iterations,
                    "tool_calls": result.tool_calls,
                    "parse_paths": result.parse_paths,
                },
            }
        )

    def event_stream() -> Iterator[str]:
        # Buffered-then-streamed: the grounded answer is already computed; emit
        # it as real SSE deltas (word-ish chunks) ending in [DONE].
        for token in _TOKEN_RE.findall(result.text) or [result.text]:
            yield _sse_chunk(result.model, result.turn_id, token)
        yield _sse_final(result.model, result.turn_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Sonar-Turn-Id": result.turn_id,
        },
    )


@app.get("/events")
def get_events(
    request: Request,
    turn_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    since: int | None = None,
) -> Any:
    """Step-event history, oldest-first, in the shape the overlay already parses.

    ``turn_id``/``limit`` behave as before; ``since`` (inclusive epoch ms) is
    the new one — a poller passes back ``last_ts + 1`` to get only what it has
    not seen. Reads are durable-first, so a Console window can ask about a turn
    that happened before the current harness process.

    Deliberately ``def``, not ``async def``: this read now reaches SQLite behind
    the store's lock — which the turn thread holds while it appends and while a
    retention sweep runs — and awaiting that inline would freeze the whole event
    loop, SSE deltas of the live answer included. Starlette runs a sync endpoint
    in its threadpool, so a slow read costs this poll and nothing else.
    """
    events: EventSink = request.app.state.events
    return JSONResponse(
        {"events": events.query(since=since, turn_id=turn_id, limit=limit)}
    )


@app.get("/health")
async def health(request: Request) -> Any:
    st = request.app.state
    return JSONResponse(
        {
            "status": "ok",
            "tools": st.registry.names(),
            "chunks": st.backend.chunk_count,
            "default_model": st.models.resolve(st.models.default),
        }
    )


@app.get("/nudges")
async def get_nudges(request: Request) -> Any:
    """"What wants my attention right now" — the SILENT, PULL-only surface.

    Nothing here speaks, notifies, or schedules: it answers because something
    asked, in that moment (``sonar_harness.nudges`` explains why that matters).
    A caller POLLS this, so two rules hold: it must be cheap — the engine keeps a
    TTL cache, so most polls are dictionary work — and it must never block the
    turn loop, hence the worker thread. On any failure it serves an EMPTY
    surface with 200: a menu bar has nowhere to render a 500.
    """
    st = request.app.state
    engine = getattr(st, "nudges", None)
    if engine is None:
        # Built lazily and cached on app.state so polling reuses one engine (and
        # one cache). No await between the read and the write, so concurrent
        # requests cannot interleave into two engines.
        engine = NudgeEngine(
            vault_path=os.environ.get("SONAR_VAULT_PATH", str(DEFAULT_VAULT))
        )
        st.nudges = engine

    try:
        snapshot = await anyio.to_thread.run_sync(engine.snapshot)
    except Exception as exc:  # noqa: BLE001 — absence is the only failure mode
        log.warning("nudges unavailable (%s: %s)", type(exc).__name__, exc)
        snapshot = empty_snapshot()
    return JSONResponse(snapshot)
