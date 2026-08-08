# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "websockets>=13",
#   "httpx>=0.27",
# ]
# ///
"""Sonar overlay bridge (typed path) — WS server bridging the overlay <-> harness /v1.

Stream C: the Hammerspoon overlay (WS client on :8770) sends ``{"text": "..."}``
when you type a question in the box; this relay runs it through the harness
(``POST /v1/chat/completions`` SSE) and streams the answer back plus the
per-turn step-events (search / note_context / synthesis / final) so the box
fills in and the expandable "steps taken" panel populates.

No STT/TTS here — that is the voice phase. This relay is the SEED of the voice
client: it already owns the overlay WS protocol and the harness round-trip, so
the voice loop later just adds mic -> STT in front and TTS -> speaker behind the
same turn.

Wire protocol (WebSocket SERVER on 127.0.0.1:8770; the glow init.lua is CLIENT):
  <- {"cmd": "start"|"stop"}          box opened/closed (glow only; acked)
  <- {"text": "<question>"}           run ONE harness turn
  -> {"turn": "start"|"end"}          bracket a turn (overlay shows/clears busy)
  -> {"state": "...", "level": n}     glow modulation
  -> {"step": {step,tool,detail,status}}   one harness step-event
  -> {"answer": "<delta>", "partial": true|false}   streamed answer text

Run:  SONAR_HARNESS_URL=http://127.0.0.1:8787 uv run overlay/bridge.py
(start the harness first: `SONAR_PORT=8787 uv run --project harness python -m sonar_harness`)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx
import websockets

log = logging.getLogger("sonar.bridge")

HOST = os.environ.get("SONAR_GLOW_HOST", "127.0.0.1")
PORT = int(os.environ.get("SONAR_GLOW_PORT", "8770"))
HARNESS_URL = os.environ.get("SONAR_HARNESS_URL", "http://127.0.0.1:8787").rstrip("/")

_SSE_DATA_PREFIX = "data: "
_SSE_DONE = "[DONE]"

# WebSockets are exempt from the browser's same-origin policy, so without an
# explicit check any page the user visits could open ws://127.0.0.1:8770 and send
# {"text": "..."} — which runs a FULL harness turn as the user, with their Gmail,
# Calendar, drafts and Obsidian vault behind it, and streams the answer straight
# back to that page. Nothing shows on screen either, because every reply goes
# only to the sender. The voice loop binds this same port when voice is running
# and already gates it; the typed path needs the identical gate.
#
# KNOWN GAP: a client that sends no Origin at all is allowed, because
# Hammerspoon's hs.websocket sends none. That means this stops hostile WEB
# PAGES, not another process running as the user. Closing that needs a shared
# token both the overlay and the bridge read.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


def allowed_origins(port: int = PORT) -> set[str]:
    """The exact local origins allowed to hand shake, on the bound port."""
    return {f"http://{host}:{port}" for host in _LOOPBACK_HOSTS}


def serve_origins() -> list:
    """``origins=`` for websockets.serve: a loopback regex backstop (any port —
    the bound port isn't known here) plus None for clients that send no Origin.
    The exact per-port check is origin_allowed(), applied in the handshake."""
    hosts = "|".join(re.escape(h) for h in _LOOPBACK_HOSTS)
    return [re.compile(rf"http://(?:{hosts})(?::\d+)?"), None]


def origin_allowed(headers: Any, port: int = PORT) -> bool:
    """True for our own local origins, or a client sending no Origin. A
    malformed or duplicated Origin header is refused."""
    try:
        origin = headers.get("Origin")
    except Exception:  # noqa: BLE001 — malformed/duplicate Origin: refuse
        return False
    if origin is None:
        return True
    return origin in allowed_origins(port)


def is_websocket_upgrade(headers: Any) -> bool:
    """True only for a real handshake: Connection carries an ``upgrade`` token
    AND Upgrade is ``websocket``. Those are the two things accept() checks (in
    that order) before it raises; anything else is a plain HTTP request."""
    try:
        connection = headers.get("Connection") or ""
        upgrade = headers.get("Upgrade") or ""
    except Exception:  # noqa: BLE001 — malformed/duplicate headers: not a handshake
        return False
    tokens = {tok.strip().lower() for tok in connection.split(",")}
    return "upgrade" in tokens and upgrade.strip().lower() == "websocket"


def _plain_response(status: Any, reason: str, body: bytes) -> Any:
    """A complete little HTTP reply. websockets serializes exactly what we hand
    it and adds nothing, so Content-Length is ours to set."""
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    return Response(
        status, reason,
        Headers([
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("X-Content-Type-Options", "nosniff"),
            ("Connection", "close"),
        ]),
        body,
    )


def make_origin_gate(port: int = PORT):
    """A ``process_request`` for websockets.serve: refuses a cross-origin
    handshake BEFORE accept(), and answers a plain HTTP request itself. Returns
    None only for a real, allowed handshake. Deliberately a twin of
    voice_loop.py's gate — both bind :8770, and each is a self-contained PEP 723
    script that cannot import from the other (test_bridge_origin pins that the
    two behave identically)."""

    def gate(_conn: Any, request: Any) -> Any:
        from http import HTTPStatus

        if not origin_allowed(request.headers, port):
            log.warning("refused cross-origin websocket on :%d", port)
            return _plain_response(
                HTTPStatus.FORBIDDEN, "Forbidden", b"cross-origin websocket refused\n"
            )
        if is_websocket_upgrade(request.headers):
            return None
        # Not a handshake. The app's liveness probe GETs this port every 5 s;
        # letting that fall through to accept() raises InvalidUpgrade, which
        # websockets logs as an ERROR plus a full traceback on EVERY poll.
        if request.path not in ("/", "/index.html"):
            return _plain_response(HTTPStatus.NOT_FOUND, "Not Found", b"not found\n")
        return _plain_response(HTTPStatus.OK, "OK", b"sonar overlay bridge\n")

    return gate


def sse_delta(line: str) -> str | None:
    """Extract ``choices[0].delta.content`` from one SSE line, or None.

    Mirrors voice/osvoice/providers/llm_openai.py so the bridge reads the harness
    stream exactly as the voice LM slot will.
    """
    if not line.startswith(_SSE_DATA_PREFIX):
        return None
    data = line[len(_SSE_DATA_PREFIX):].strip()
    if not data or data == _SSE_DONE:
        return None
    try:
        obj = json.loads(data)
        choices = obj.get("choices") or []
        if not choices:
            return None
        return choices[0].get("delta", {}).get("content")
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
        return None


async def _send(ws, msg: dict) -> None:
    """Best-effort JSON send; a dropped socket ends the turn, not the process."""
    try:
        await ws.send(json.dumps(msg))
    except Exception:
        raise ConnectionError("overlay socket closed")


async def run_turn(ws, client: httpx.AsyncClient, text: str) -> None:
    """Drive one harness turn: stream the answer + relay its step-events."""
    await _send(ws, {"turn": "start"})
    await _send(ws, {"state": "thinking", "level": 0.6})
    payload = {"stream": True, "messages": [{"role": "user", "content": text}]}
    try:
        async with client.stream(
            "POST", "/v1/chat/completions", json=payload, timeout=180.0
        ) as resp:
            resp.raise_for_status()
            # The harness runs the whole (blocking) tool loop before it streams a
            # byte, so by the time headers arrive every step-event is recorded.
            turn_id = resp.headers.get("X-Sonar-Turn-Id")
            await _relay_steps(ws, client, turn_id)
            async for line in resp.aiter_lines():
                delta = sse_delta(line)
                if delta:
                    await _send(ws, {"answer": delta, "partial": True})
    except httpx.HTTPError as exc:
        log.exception("harness turn failed")
        await _send(
            ws,
            {"answer": f"[harness unreachable at {HARNESS_URL}: {exc}]", "partial": True},
        )
    finally:
        await _send(ws, {"answer": "", "partial": False})
        await _send(ws, {"turn": "end"})
        await _send(ws, {"state": "listening", "level": 0.2})


async def _relay_steps(ws, client: httpx.AsyncClient, turn_id: str | None) -> None:
    """Fetch this turn's step-events from the harness and forward each."""
    if not turn_id:
        return
    try:
        r = await client.get("/events", params={"turn_id": turn_id}, timeout=10.0)
        r.raise_for_status()
        for ev in r.json().get("events", []):
            await _send(ws, {"step": ev})
    except httpx.HTTPError:
        log.warning("could not fetch /events for turn %s", turn_id)


async def handler(ws) -> None:
    log.info("overlay connected")
    async with httpx.AsyncClient(base_url=HARNESS_URL) as client:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            text = msg.get("text")
            cmd = msg.get("cmd")
            if isinstance(text, str) and text.strip():
                try:
                    await run_turn(ws, client, text.strip())
                except ConnectionError:
                    return  # overlay went away mid-turn
            elif cmd == "start":
                await _send(ws, {"state": "listening", "level": 0.2})
            elif cmd == "stop":
                await _send(ws, {"state": "idle", "level": 0.0})


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    log.info("bridge -> harness %s ; serving overlay ws://%s:%d", HARNESS_URL, HOST, PORT)
    async with websockets.serve(
        handler, HOST, PORT,
        origins=serve_origins(), process_request=make_origin_gate(PORT),
    ):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[bridge] bye", flush=True)
