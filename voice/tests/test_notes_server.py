"""Notes server: page over HTTP, state sync + ops over WS, broadcast fan-out."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import websockets

from notes.server import NotesServer, is_websocket_upgrade


class FakeOps:
    """Minimal NotesOps: records every client op, serves a canned snapshot."""

    def __init__(self) -> None:
        self.received: list[dict] = []

    def state_json(self) -> dict:
        return {"type": "state", "rev": 0, "status": "recording", "title": "t"}

    async def apply_client_op(self, msg: dict) -> None:
        self.received.append(msg)


@pytest.fixture
async def served():
    ops = FakeOps()
    server = NotesServer(ops, port=0)          # ephemeral port
    await server.start()
    try:
        yield ops, server
    finally:
        await server.stop()


def test_upgrade_detection_is_defensive() -> None:
    assert is_websocket_upgrade({"Upgrade": "websocket"})
    assert is_websocket_upgrade({"Upgrade": " WebSocket "})
    assert not is_websocket_upgrade({"Upgrade": "h2c"})
    assert not is_websocket_upgrade({})
    assert not is_websocket_upgrade(None)


async def test_plain_http_serves_the_ui(served) -> None:
    _ops, server = served
    async with httpx.AsyncClient() as client:
        resp = await client.get(server.url + "/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Sonar Notes" in resp.text
        missing = await client.get(server.url + "/nope")
        assert missing.status_code == 404


async def test_ws_gets_state_then_forwards_ops(served) -> None:
    ops, server = served
    async with websockets.connect(f"ws://127.0.0.1:{server.port}/ws") as ws:
        first = json.loads(await ws.recv())
        assert first == ops.state_json()

        await ws.send(json.dumps({"op": "set_title", "title": "new"}))
        await ws.send("not json")                       # ignored, not fatal
        await ws.send(json.dumps(["not", "a", "dict"]))  # ignored, not fatal
        await ws.send(json.dumps({"op": "end"}))
        for _ in range(50):
            if len(ops.received) >= 2:
                break
            await asyncio.sleep(0.02)
    assert ops.received == [{"op": "set_title", "title": "new"}, {"op": "end"}]


async def test_broadcast_reaches_every_client_and_skips_dead_ones(served) -> None:
    _ops, server = served
    async with websockets.connect(f"ws://127.0.0.1:{server.port}/ws") as a, \
               websockets.connect(f"ws://127.0.0.1:{server.port}/ws") as b:
        await a.recv(), await b.recv()                  # initial snapshots
        await b.close()
        await server.broadcast({"type": "partial", "text": "hello"})
        got: Any = json.loads(await asyncio.wait_for(a.recv(), timeout=2))
        assert got == {"type": "partial", "text": "hello"}


async def test_cross_origin_ws_handshake_is_refused(served) -> None:
    # #2 regression: WS is exempt from CORS, so a tab on evil.com could otherwise
    # open ws://127.0.0.1:<port> and read/drive the live meeting. The server must
    # reject a foreign Origin at the handshake (HTTP 403).
    _ops, server = served
    uri = f"ws://127.0.0.1:{server.port}/ws"
    with pytest.raises(websockets.InvalidStatus) as exc:
        async with websockets.connect(uri, origin="https://evil.com"):
            pass  # pragma: no cover — handshake should already have failed
    assert exc.value.response.status_code == 403


async def test_same_origin_ws_handshake_is_accepted_and_gets_state(served) -> None:
    # The page's own loopback origin (on the bound port) still handshakes and
    # receives the initial state snapshot — the gate didn't lock out real users.
    ops, server = served
    uri = f"ws://127.0.0.1:{server.port}/ws"
    async with websockets.connect(uri, origin=f"http://127.0.0.1:{server.port}") as ws:
        first = json.loads(await ws.recv())
    assert first == ops.state_json()
