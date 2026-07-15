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

import asyncio
import json
import logging
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("sonar.notes.server")

_UI_PATH = Path(__file__).with_name("ui.html")

# A backgrounded/suspended tab that stops draining applies write backpressure;
# bound every broadcast send so one such client can't freeze the pipeline worker.
_SEND_TIMEOUT_S = 2.0


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

        # Defence in depth: accept() re-checks `origins=` after _process_request
        # returns, so it must not reject what the exact gate allowed. The bound
        # port is unknown here (self._port may be 0), so match loopback by host
        # on any port and leave the exact-port decision to _process_request.
        # None permits non-browser clients (curl, the round-trip tests) that
        # send no Origin — browsers always send one, so evil.com can't spoof it.
        self._server = await websockets.serve(
            self._handler, self._host, self._port,
            process_request=self._process_request,
            origins=self._serve_origins(),
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
        """Best-effort fan-out; a dead OR slow connection is dropped, never
        fatal. Sends run concurrently and each is bounded by a short timeout, so
        a backgrounded tab that stops draining can't serialize-block the others
        or freeze the single pipeline worker (which would then hang end()'s
        queue.join() on write backpressure)."""
        data = json.dumps(msg)
        conns = list(self._conns)
        if not conns:
            return

        async def _send(conn: Any) -> None:
            try:
                await asyncio.wait_for(conn.send(data), timeout=_SEND_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — slow/closed/broken socket: drop it
                self._conns.discard(conn)

        await asyncio.gather(*(_send(conn) for conn in conns))

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
        """Serve the UI page for plain HTTP; let same-origin WS handshakes
        through (None). A cross-origin WS upgrade — a tab on evil.com reaching
        for ws://127.0.0.1:{port} to read/drive the live meeting — is refused
        with 403 here: WS is exempt from CORS, so the server must gate it (this
        runs before accept(), using the actual bound port)."""
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        if is_websocket_upgrade(request.headers):
            if self._origin_allowed(request.headers):
                return None
            return Response(HTTPStatus.FORBIDDEN, "Forbidden",
                            Headers([("Content-Type", "text/plain")]),
                            b"cross-origin websocket refused\n")

        if request.path not in ("/", "/index.html"):
            return Response(HTTPStatus.NOT_FOUND, "Not Found",
                            Headers([("Content-Type", "text/plain")]), b"not found\n")
        body = self._page()
        headers = Headers([
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            # frame-ancestors 'none' keeps the page out of any <iframe>, so
            # Discard/End/Save can't be clickjacked from a framing site.
            ("Content-Security-Policy",
             "default-src 'none'; style-src 'unsafe-inline'; "
             "script-src 'unsafe-inline'; connect-src ws: http:; img-src data:; "
             "frame-ancestors 'none'"),
        ])
        return Response(HTTPStatus.OK, "OK", headers, body)

    def _loopback_hosts(self) -> list[str]:
        """The host the page was served as plus the loopback spellings a user
        might open it as (deduped, order preserved)."""
        return list(dict.fromkeys((self._host, "127.0.0.1", "localhost")))

    def _allowed_origins(self) -> set[str]:
        """The exact local origins the served page may hand shake from — its own
        host on the *bound* port, plus loopback spellings of it."""
        port = self.port
        return {f"http://{host}:{port}" for host in self._loopback_hosts()}

    def _serve_origins(self) -> list:
        """`origins=` for websockets.serve: a loopback-host regex (any port,
        since the bound port isn't known yet) as a backstop, plus None to allow
        a missing Origin. The tight per-port check lives in _process_request."""
        hosts = "|".join(re.escape(h) for h in self._loopback_hosts())
        return [re.compile(rf"http://(?:{hosts})(?::\d+)?"), None]

    def _origin_allowed(self, headers: Any) -> bool:
        """True for our own local origins, or for a client that sends no Origin
        at all (curl / the round-trip tests). A malformed or duplicated Origin
        header is refused."""
        try:
            origin = headers.get("Origin")
        except Exception:  # noqa: BLE001 — malformed/duplicate Origin: refuse
            return False
        if origin is None:
            return True
        return origin in self._allowed_origins()

    def _page(self) -> bytes:
        if self._html is None:
            self._html = _UI_PATH.read_bytes()
        return self._html
