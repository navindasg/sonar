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

import httpx
import websockets

log = logging.getLogger("sonar.bridge")

HOST = os.environ.get("SONAR_GLOW_HOST", "127.0.0.1")
PORT = int(os.environ.get("SONAR_GLOW_PORT", "8770"))
HARNESS_URL = os.environ.get("SONAR_HARNESS_URL", "http://127.0.0.1:8787").rstrip("/")

_SSE_DATA_PREFIX = "data: "
_SSE_DONE = "[DONE]"


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
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[bridge] bye", flush=True)
