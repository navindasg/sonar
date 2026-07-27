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
import re

import pytest
import websockets

from voice_loop import (
    allowed_origins,
    make_origin_gate,
    origin_allowed,
    serve_origins,
)


class _Headers:
    """Mimics the websockets Headers.get contract, including its raising
    behaviour on a duplicated header."""

    def __init__(self, value: str | None = None, *, duplicated: bool = False) -> None:
        self._value = value
        self._duplicated = duplicated

    def get(self, _name: str) -> str | None:
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
    def __init__(self, headers: _Headers) -> None:
        self.headers = headers


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
