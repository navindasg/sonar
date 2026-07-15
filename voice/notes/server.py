"""Notes UI transport: one port serves the page (HTTP) and its live feed (WS).

Runs inside the voice-loop process on :8771 (SONAR_NOTES_PORT) — the browser
GETs `/` for the single-file UI, then upgrades a WebSocket on the same port.
Both come through `websockets.serve`: a plain HTTP request is answered from
`process_request` (no extra web framework), an Upgrade request proceeds to the
WS handler.

The server is a dumb pipe: on connect it sends the controller's full session
snapshot, then forwards each client op (rename / edit / end / save / …) to
`controller.apply_client_op`, which owns all state and broadcasts back through
`broadcast`. Loopback-only by default; the page is served with a
no-external-origins CSP.
"""
from __future__ import annotations

import json
import logging
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("sonar.notes.server")

_UI_PATH = Path(__file__).with_name("ui.html")


class NotesOps(Protocol):
    """What the server needs from the controller (kept minimal for tests)."""

    def state_json(self) -> dict: ...
    async def apply_client_op(self, msg: dict) -> None: ...


def is_websocket_upgrade(headers: Any) -> bool:
    """True for a WS handshake; anything else is served the page (pure)."""
    try:
        upgrade = headers.get("Upgrade") or ""
    except Exception:  # noqa: BLE001 — malformed headers are a plain request
        return False
    return upgrade.strip().lower() == "websocket"


class NotesServer:
    """WebSocket + static-page server for one controller."""

    def __init__(self, ops: NotesOps, host: str = "127.0.0.1", port: int = 8771) -> None:
        self._ops = ops
        self._host = host
        self._port = port
        self._server: Any | None = None
        self._conns: set[Any] = set()
        self._html: bytes | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}"

    @property
    def port(self) -> int:
        """The bound port (differs from the requested one when that was 0)."""
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self._port

    async def start(self) -> None:
        """Bind the port (idempotent). Import websockets lazily so pure-logic
        notes tests don't need it installed."""
        if self._server is not None:
            return
        import websockets

        self._server = await websockets.serve(
            self._handler, self._host, self._port,
            process_request=self._process_request,
        )
        log.info("notes UI on %s", self.url)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        self._conns.clear()

    async def broadcast(self, msg: dict) -> None:
        """Best-effort fan-out; a dead connection is dropped, never fatal."""
        data = json.dumps(msg)
        for conn in list(self._conns):
            try:
                await conn.send(data)
            except Exception:  # noqa: BLE001 — closed/broken socket
                self._conns.discard(conn)

    async def _handler(self, ws: Any) -> None:
        self._conns.add(ws)
        try:
            await ws.send(json.dumps(self._ops.state_json()))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(msg, dict):
                    try:
                        await self._ops.apply_client_op(msg)
                    except Exception:  # noqa: BLE001 — one bad op must not drop the feed
                        log.exception("client op failed: %r", msg)
        except Exception:  # noqa: BLE001 — connection torn down mid-read
            pass
        finally:
            self._conns.discard(ws)

    def _process_request(self, conn: Any, request: Any) -> Any:
        """Serve the UI page for plain HTTP; let WS handshakes through (None)."""
        if is_websocket_upgrade(request.headers):
            return None
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        if request.path not in ("/", "/index.html"):
            return Response(HTTPStatus.NOT_FOUND, "Not Found",
                            Headers([("Content-Type", "text/plain")]), b"not found\n")
        body = self._page()
        headers = Headers([
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("Content-Security-Policy",
             "default-src 'none'; style-src 'unsafe-inline'; "
             "script-src 'unsafe-inline'; connect-src ws: http:; img-src data:"),
        ])
        return Response(HTTPStatus.OK, "OK", headers, body)

    def _page(self) -> bytes:
        if self._html is None:
            self._html = _UI_PATH.read_bytes()
        return self._html
