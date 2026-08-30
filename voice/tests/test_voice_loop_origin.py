"""Only the local overlay may drive the voice socket on :8770.

WebSockets are exempt from the browser's same-origin policy, so without an
explicit check any page the user visits can open ws://127.0.0.1:8770 and send
``{"cmd":"start"}`` (mic on, and the listening ack goes only to the sender, so
nothing appears on screen) or ``{"cmd":"say"}`` (arbitrary speech out the
speakers). :8771 already gates this in ``notes/server.py``; these pin the same
gate on :8770.

Scope, stated honestly: this closes the BROWSER door. A local process that sends
no Origin header at all is still allowed through — same as the notes server —
because Hammerspoon's hs.websocket and morning_brief.py send no Origin. Closing
that door needs a shared token, which is a separate change.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
import pytest
import websockets

from voice_loop import (
    allowed_origins,
    is_websocket_upgrade,
    make_origin_gate,
    origin_allowed,
    serve_origins,
)


class _Headers:
    """Mimics the websockets Headers.get contract — per-NAME lookups, and its
    raising behaviour on a duplicated header. Defaults describe a real
    handshake, so an Origin-only test reads as one."""

    def __init__(
        self,
        value: str | None = None,
        *,
        duplicated: bool = False,
        upgrade: str | None = "websocket",
        connection: str | None = "Upgrade",
    ) -> None:
        self._value = value
        self._duplicated = duplicated
        self._upgrade = upgrade
        self._connection = connection

    def get(self, name: str) -> str | None:
        if name == "Upgrade":
            return self._upgrade
        if name == "Connection":
            return self._connection
        if self._duplicated:
            raise ValueError("duplicate Origin header")
        return self._value


def test_our_own_page_origins_are_allowed() -> None:
    for origin in allowed_origins(8770):
        assert origin_allowed(_Headers(origin), 8770) is True


def test_loopback_spellings_are_covered() -> None:
    assert "http://127.0.0.1:8770" in allowed_origins(8770)
    assert "http://localhost:8770" in allowed_origins(8770)


def test_a_hostile_page_is_refused() -> None:
    assert origin_allowed(_Headers("http://evil.com"), 8770) is False
    assert origin_allowed(_Headers("https://evil.com"), 8770) is False


def test_a_lookalike_host_is_refused() -> None:
    """127.0.0.1.evil.com and localhost.evil.com must not slip through."""
    assert origin_allowed(_Headers("http://127.0.0.1.evil.com"), 8770) is False
    assert origin_allowed(_Headers("http://localhost.evil.com:8770"), 8770) is False


def test_a_different_local_port_is_refused() -> None:
    """The notes UI on :8771 has no business driving the mic on :8770."""
    assert origin_allowed(_Headers("http://127.0.0.1:8771"), 8770) is False


def test_a_malformed_or_duplicated_origin_is_refused() -> None:
    assert origin_allowed(_Headers(duplicated=True), 8770) is False


def test_no_origin_is_allowed() -> None:
    """Hammerspoon's hs.websocket and morning_brief.py send no Origin at all —
    refusing them would break F5. This is the documented gap the token would
    close."""
    assert origin_allowed(_Headers(None), 8770) is True


def test_serve_origins_backstops_loopback_and_none() -> None:
    origins = serve_origins()
    assert None in origins, "a missing Origin must still reach the handler"
    pattern = next(o for o in origins if isinstance(o, re.Pattern))
    assert pattern.fullmatch("http://127.0.0.1:8770")
    assert pattern.fullmatch("http://localhost")
    assert not pattern.fullmatch("http://evil.com")
    assert not pattern.fullmatch("http://127.0.0.1.evil.com")


# ---- the gate as websockets.serve actually uses it ---------------------------
def test_gate_lets_our_own_connections_through() -> None:
    gate = make_origin_gate(8770)
    assert gate(None, _Request(_Headers(None))) is None            # Hammerspoon
    assert gate(None, _Request(_Headers("http://127.0.0.1:8770"))) is None


def test_gate_returns_403_for_a_hostile_page() -> None:
    resp = make_origin_gate(8770)(None, _Request(_Headers("http://evil.com")))
    assert resp is not None
    assert resp.status_code == 403


class _Request:
    def __init__(self, headers: _Headers, path: str = "/") -> None:
        self.headers = headers
        self.path = path


# ---- plain HTTP must NOT fall through to accept() ----------------------------
# The app's ServiceProbe GETs this port every 5 s with `Connection: keep-alive`
# and no Upgrade. Falling through to accept() raises InvalidUpgrade, which
# websockets logs as an ERROR plus a ~10-line traceback on EVERY poll — ~17k a
# day, which is what buried the real lines the night the loop died overnight
# and a notes session lost its transcript.
def test_upgrade_detection_needs_both_headers() -> None:
    assert is_websocket_upgrade(_Headers(connection="Upgrade", upgrade="websocket"))
    assert is_websocket_upgrade(_Headers(connection="keep-alive, Upgrade",
                                         upgrade="WebSocket"))
    assert not is_websocket_upgrade(_Headers(connection="keep-alive", upgrade=None))
    # Upgrade without the Connection token is what accept() rejects FIRST.
    assert not is_websocket_upgrade(_Headers(connection="keep-alive",
                                             upgrade="websocket"))
    assert not is_websocket_upgrade(_Headers(connection="Upgrade", upgrade="h2c"))


def test_gate_answers_a_plain_probe_instead_of_falling_through() -> None:
    gate = make_origin_gate(8770)
    probe = _Request(_Headers(None, connection="keep-alive", upgrade=None))
    resp = gate(None, probe)
    assert resp is not None, "a plain GET must be answered here, not by accept()"
    assert resp.status_code == 200
    assert resp.headers["Content-Length"] == str(len(resp.body))


def test_gate_404s_an_unknown_path() -> None:
    gate = make_origin_gate(8770)
    resp = gate(None, _Request(_Headers(None, connection="keep-alive", upgrade=None),
                               path="/admin"))
    assert resp.status_code == 404


# ---- real handshake, real server --------------------------------------------
async def _serve(handler):
    """Bind the gate exactly as main() does, on an ephemeral port."""
    return await websockets.serve(
        handler, "127.0.0.1", 0,
        origins=serve_origins(), process_request=make_origin_gate(0),
    )


async def test_a_no_origin_client_really_connects() -> None:
    """The F5 path: Hammerspoon sends no Origin and MUST still get through —
    this is the test that catches the gate breaking the overlay."""
    seen: list[str] = []

    async def handler(ws):
        seen.append(await ws.recv())

    server = await _serve(handler)
    try:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send('{"cmd":"start"}')
            await asyncio.sleep(0.05)
    finally:
        server.close()
        await server.wait_closed()

    assert seen == ['{"cmd":"start"}']


async def test_a_liveness_probe_gets_200_and_logs_no_traceback(caplog) -> None:
    """The regression that made the voice log ~90% noise: a plain GET fell
    through to accept(), which raised InvalidUpgrade and logged ERROR + a full
    traceback every 5 seconds. :8771 has always behaved; :8770 must too."""

    async def handler(ws):  # pragma: no cover — a probe never reaches the handler
        raise AssertionError("a plain HTTP GET reached the WS handler")

    server = await _serve(handler)
    try:
        port = server.sockets[0].getsockname()[1]
        with caplog.at_level(logging.ERROR, logger="websockets.server"):
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/")
        assert resp.status_code == 200
        assert resp.text.strip()
    finally:
        server.close()
        await server.wait_closed()

    assert caplog.records == [], (
        f"the probe still logs errors: {[r.getMessage() for r in caplog.records]}"
    )


async def test_a_hostile_origin_is_really_refused() -> None:
    """A page on evil.com reaching for the mic gets 403 at the handshake."""

    async def handler(ws):  # pragma: no cover — must never be reached
        raise AssertionError("hostile origin reached the handler")

    server = await _serve(handler)
    try:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            async with websockets.connect(
                f"ws://127.0.0.1:{port}",
                additional_headers={"Origin": "http://evil.com"},
            ):
                pass
        assert exc.value.response.status_code == 403
    finally:
        server.close()
        await server.wait_closed()
