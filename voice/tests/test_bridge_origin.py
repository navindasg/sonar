"""Only the local overlay may drive the bridge socket on :8770.

Same gap, same fix as ``voice/tests/test_voice_loop_origin.py`` — the bridge is
the OTHER server that binds :8770 (the typed F5 path; the voice loop binds it
when voice is running instead). WebSockets are exempt from the browser's
same-origin policy, so without an explicit check any page the user visits can
open ws://127.0.0.1:8770 and POST ``{"text": "..."}`` — which runs a full
harness turn as the user, with their Gmail, Calendar, drafts and vault behind
it, and streams the answer straight back to the page. Nothing appears on screen,
because every reply goes only to the sender.

Scope, stated honestly: this closes the BROWSER door. A local process that sends
no Origin header at all is still allowed through — because Hammerspoon's
hs.websocket sends none, and refusing it would break F5. Closing that door needs
a shared token, which is a separate change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

import httpx
import pytest
import websockets

# bridge.py lives in overlay/, which is not a package and not on the voice
# path — add it so this test is collected by the voice suite (testpaths =
# ["tests"]) instead of sitting in overlay/ where nothing would ever run it.
_OVERLAY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "overlay",
)
sys.path.insert(0, _OVERLAY)

import bridge  # noqa: E402
from bridge import (  # noqa: E402
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


class _Request:
    def __init__(self, headers: _Headers, path: str = "/") -> None:
        self.headers = headers
        self.path = path


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
    """The notes UI on :8771 has no business running harness turns on :8770."""
    assert origin_allowed(_Headers("http://127.0.0.1:8771"), 8770) is False


def test_a_malformed_or_duplicated_origin_is_refused() -> None:
    assert origin_allowed(_Headers(duplicated=True), 8770) is False


def test_no_origin_is_allowed() -> None:
    """Hammerspoon's hs.websocket sends no Origin at all — refusing it would
    break F5. This is the documented gap the token would close."""
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


# ---- plain HTTP must NOT fall through to accept() ----------------------------
# `sonar.sh up` runs the bridge on :8770 whenever voice isn't up, and the app
# probes the port every 5 s regardless of who owns it. Without this branch the
# probe reaches accept(), raises InvalidUpgrade and logs ERROR + a full
# traceback each time — the same flood as the voice loop, in the bridge log.
def test_upgrade_detection_needs_both_headers() -> None:
    assert is_websocket_upgrade(_Headers(connection="Upgrade", upgrade="websocket"))
    assert is_websocket_upgrade(_Headers(connection="keep-alive, Upgrade",
                                         upgrade="WebSocket"))
    assert not is_websocket_upgrade(_Headers(connection="keep-alive", upgrade=None))
    assert not is_websocket_upgrade(_Headers(connection="keep-alive",
                                             upgrade="websocket"))
    assert not is_websocket_upgrade(_Headers(connection="Upgrade", upgrade="h2c"))


def test_gate_answers_a_plain_probe_instead_of_falling_through() -> None:
    resp = make_origin_gate(8770)(
        None, _Request(_Headers(None, connection="keep-alive", upgrade=None))
    )
    assert resp is not None, "a plain GET must be answered here, not by accept()"
    assert resp.status_code == 200
    assert resp.headers["Content-Length"] == str(len(resp.body))


def test_gate_404s_an_unknown_path() -> None:
    resp = make_origin_gate(8770)(
        None,
        _Request(_Headers(None, connection="keep-alive", upgrade=None), path="/admin"),
    )
    assert resp.status_code == 404


def test_both_8770_gates_agree_on_a_plain_probe() -> None:
    """bridge.py and voice_loop.py each bind :8770 and each carry their own copy
    of this gate (two self-contained PEP 723 scripts — neither can import the
    other). Fixing one and not the other is exactly how the noise comes back."""
    import voice_loop

    probe = _Request(_Headers(None, connection="keep-alive", upgrade=None))
    hostile = _Request(_Headers("http://evil.com"))
    for gate in (make_origin_gate(8770), voice_loop.make_origin_gate(8770)):
        assert gate(None, probe).status_code == 200
        assert gate(None, hostile).status_code == 403
        assert gate(None, _Request(_Headers(None))) is None   # the F5 handshake


# ---- real handshake, real server --------------------------------------------
# Written as sync tests driving asyncio.run() on purpose: overlay/ has no pytest
# config of its own, so there is no asyncio_mode=auto to lean on the way
# voice/tests does. These must run under a bare pytest with no plugins.
async def _serve(handler):
    """Bind the gate exactly as main() does, on an ephemeral port."""
    return await websockets.serve(
        handler, "127.0.0.1", 0,
        origins=serve_origins(), process_request=make_origin_gate(0),
    )


def test_a_no_origin_client_really_connects() -> None:
    """The F5 path: Hammerspoon sends no Origin and MUST still get through —
    this is the test that catches the gate breaking the overlay."""
    seen: list[str] = []

    async def scenario() -> None:
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

    asyncio.run(scenario())
    assert seen == ['{"cmd":"start"}']


def test_a_liveness_probe_gets_200_and_logs_no_traceback(caplog) -> None:
    """The app's 5 s probe must be answered, not tracebacked — same regression
    guard as the voice loop's, on the process that owns :8770 without voice."""

    async def scenario() -> None:
        async def handler(ws):  # pragma: no cover — a probe never reaches it
            raise AssertionError("a plain HTTP GET reached the WS handler")

        server = await _serve(handler)
        try:
            port = server.sockets[0].getsockname()[1]
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/")
            assert resp.status_code == 200
            assert resp.text.strip()
        finally:
            server.close()
            await server.wait_closed()

    with caplog.at_level(logging.ERROR, logger="websockets.server"):
        asyncio.run(scenario())

    assert caplog.records == [], (
        f"the probe still logs errors: {[r.getMessage() for r in caplog.records]}"
    )


def test_a_hostile_origin_is_really_refused() -> None:
    """A page on evil.com reaching for a harness turn gets 403 at the
    handshake, before the handler ever sees it."""

    async def scenario() -> None:
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

    asyncio.run(scenario())


def test_main_actually_binds_the_gate_to_the_real_server(monkeypatch) -> None:
    """The helpers existing is worth nothing if main() forgets to pass them.

    voice_loop's suite has no equivalent, and "gate written, never wired" is the
    exact way this regresses — so assert on the real serve() call rather than on
    a reconstruction of it.
    """
    captured: dict = {}

    class _Stop(Exception):
        pass

    def fake_serve(_handler, _host, _port, **kwargs):
        captured.update(kwargs)
        raise _Stop

    monkeypatch.setattr(bridge.websockets, "serve", fake_serve)
    with pytest.raises(_Stop):
        asyncio.run(bridge.main())

    assert captured["process_request"] is not None
    assert None in captured["origins"], "no-Origin clients must still reach the handler"
    gate = captured["process_request"]
    assert gate(None, _Request(_Headers("http://evil.com"))).status_code == 403
    assert gate(None, _Request(_Headers(None))) is None
