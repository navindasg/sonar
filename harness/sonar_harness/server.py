"""OpenAI-compatible /v1 server — the STT<->TTS seam.

Exposes ``POST /v1/chat/completions`` with SSE streaming shaped EXACTLY as
``voice/osvoice/providers/llm_openai.py`` parses it:

    data: {"choices":[{"delta":{"content":"..."}}]}
    ...
    data: [DONE]

The tool loop runs NON-streaming (``agent.run_turn``); the grounded final
answer is then emitted as real SSE deltas (buffered-then-streamed — see the
design note in the task return). Step-events for the turn are exposed at
``GET /events`` so the overlay can render the "steps taken" timeline.

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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from sonar_harness.agent import run_turn
from sonar_harness.events import EventSink
from sonar_harness.model_router import load_config as load_models_config
from sonar_harness.ollama_client import DEFAULT_OLLAMA_URL, OllamaChat
from sonar_harness.prompt import load_charter
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
    app.state.events = EventSink()
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
    try:
        yield
    finally:
        app.state.ollama.close()
        app.state.state.close()


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
    import anyio

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
async def get_events(request: Request, turn_id: str | None = None, limit: int = 100) -> Any:
    events: EventSink = request.app.state.events
    return JSONResponse({"events": events.recent(turn_id=turn_id, limit=limit)})


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
