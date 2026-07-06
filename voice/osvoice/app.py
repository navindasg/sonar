"""FastAPI application factory wiring providers to the WebSocket transport.

`build_app` takes the three already-constructed (but not yet loaded) providers,
loads them once during the lifespan startup (each `load()` also warms), exposes a
health check, mounts the ``/ws`` voice endpoint, and serves the static web client
from ``clients/web`` when that directory exists. Per-connection `Pipeline`
instances are built from the providers stored on ``app.state`` so they share the
single loaded model set across all connections.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from osvoice.contracts import LLMProvider, STTProvider, TTSProvider
from osvoice.pipeline import Pipeline
from osvoice.transport_ws import EmitFn, websocket_endpoint

if TYPE_CHECKING:  # FastAPI types for checkers only.
    from fastapi import FastAPI

logger = logging.getLogger("osvoice.app")

# Static web client, relative to the repo root (parent of the package dir).
_WEB_CLIENT_DIR = Path(__file__).resolve().parent.parent / "clients" / "web"


def build_app(providers: dict[str, object]) -> FastAPI:
    """Construct the FastAPI app from ``{"stt","lm","tts"}`` provider instances.

    Validates the slot keys eagerly so a misconfigured server fails at build
    time rather than on the first connection.
    """
    from fastapi import FastAPI, WebSocket

    stt, lm, tts = _unpack_providers(providers)

    app = FastAPI(title="osvoice", lifespan=_make_lifespan(stt, lm, tts))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket_endpoint(websocket, _pipeline_factory(websocket.app))

    _mount_web_client(app)
    return app


def _unpack_providers(
    providers: dict[str, object],
) -> tuple[STTProvider, LLMProvider, TTSProvider]:
    """Pull the three slot providers out of the dict, raising on any missing key."""
    missing = {"stt", "lm", "tts"} - providers.keys()
    if missing:
        raise ValueError(f"providers dict missing slots: {sorted(missing)}")
    return (
        providers["stt"],  # type: ignore[return-value]
        providers["lm"],   # type: ignore[return-value]
        providers["tts"],  # type: ignore[return-value]
    )


def _make_lifespan(stt: STTProvider, lm: LLMProvider, tts: TTSProvider):
    """Build an async lifespan that loads providers on startup, closes on shutdown."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await _load_all(stt, lm, tts)
        app.state.stt = stt
        app.state.lm = lm
        app.state.tts = tts
        logger.info("providers loaded; osvoice ready")
        try:
            yield
        finally:
            await _close_all(stt, lm, tts)

    return lifespan


async def _load_all(stt: STTProvider, lm: LLMProvider, tts: TTSProvider) -> None:
    """Load (and warm) every provider in turn; a failure aborts startup loudly."""
    for name, provider in (("stt", stt), ("lm", lm), ("tts", tts)):
        try:
            await provider.load()
        except Exception as exc:
            logger.exception("failed to load %s provider", name)
            raise RuntimeError(f"{name} provider failed to load: {exc}") from exc


async def _close_all(stt: STTProvider, lm: LLMProvider, tts: TTSProvider) -> None:
    """Close every provider, logging (not raising) per-provider close failures."""
    for name, provider in (("stt", stt), ("lm", lm), ("tts", tts)):
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            logger.warning("error closing %s provider", name, exc_info=True)


def _pipeline_factory(app: FastAPI):
    """Return a factory that builds a Pipeline from this app's loaded providers."""

    def make_pipeline(emit: EmitFn) -> Pipeline:
        state = app.state
        return Pipeline(stt=state.stt, lm=state.lm, tts=state.tts, emit=emit)

    return make_pipeline


def _mount_web_client(app: FastAPI) -> None:
    """Serve the static web client at ``/`` when ``clients/web`` exists."""
    if not _WEB_CLIENT_DIR.is_dir():
        logger.info("no web client at %s; skipping static mount", _WEB_CLIENT_DIR)
        return
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_WEB_CLIENT_DIR), html=True), name="web")
    logger.info("serving web client from %s", _WEB_CLIENT_DIR)
