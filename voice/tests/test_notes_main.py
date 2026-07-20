"""Standalone `python -m notes` launcher: builds a mic-less NotesController from
the SONAR_* env, and serve() stands up :8771 + writes notes.url, then tears the
server down on stop. No mic/STT/embedder/torch — the null embedder + stub
transcribe keep it hermetic (websockets is the only runtime dep, a dev extra)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from notes import __main__ as notes_main
from notes.controller import NotesController


async def test_stub_transcribe_returns_empty() -> None:
    # No STT in the standalone session; the mic path is never invoked, but if it
    # were, it must yield "" (an empty, not phantom, transcript).
    assert await notes_main._stub_transcribe(b"\x00\x00") == ""


async def test_null_embedder_is_a_noop() -> None:
    emb = notes_main._NullEmbedder()
    await emb.load()                       # no torch/speechbrain download
    assert await emb.embed(b"\x00" * 4) is None
    assert await emb.embed_windows(b"\x00" * 4) == []
    await emb.aclose()


def test_build_controller_reads_env(monkeypatch, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    monkeypatch.setenv("SONAR_NOTES_PORT", "9911")
    monkeypatch.setenv("SONAR_VAULT_PATH", str(vault))
    monkeypatch.setenv("SONAR_OLLAMA_URL", "http://127.0.0.1:12345")

    ctl = notes_main.build_controller()

    assert isinstance(ctl, NotesController)
    assert ctl.server.port == 9911          # not yet bound -> requested port
    assert ctl._vault == vault
    assert ctl._ollama_url == "http://127.0.0.1:12345"
    # open_browser left at default True so _publish_url always writes notes.url.
    assert ctl._open_browser is True
    assert isinstance(ctl._embedder, notes_main._NullEmbedder)


def test_notes_port_falls_back_on_garbage(monkeypatch) -> None:
    monkeypatch.setenv("SONAR_NOTES_PORT", "not-a-port")
    assert notes_main._notes_port() == notes_main._DEFAULT_NOTES_PORT


def test_defaults_when_env_unset(monkeypatch) -> None:
    for var in ("SONAR_NOTES_PORT", "SONAR_VAULT_PATH", "SONAR_OLLAMA_URL"):
        monkeypatch.delenv(var, raising=False)
    assert notes_main._notes_port() == 8771
    assert notes_main._ollama_url() == "http://127.0.0.1:11434"
    assert notes_main._vault_path() == Path("~/Documents/Obsidian Vault").expanduser()


async def test_serve_binds_publishes_url_then_closes(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "sonar-home"
    monkeypatch.setenv("SONAR_HOME", str(home))
    monkeypatch.setenv("SONAR_NOTES_OPEN", "0")   # never shell out to `open`
    monkeypatch.setenv("SONAR_NOTES_PORT", "0")   # ephemeral port
    monkeypatch.setenv("SONAR_VAULT_PATH", str(tmp_path / "vault"))

    controller = notes_main.build_controller()
    stop = asyncio.Event()
    task = asyncio.create_task(notes_main.serve(controller, stop))
    try:
        # Wait until start() has bound the port + published notes.url.
        url_file = home / "run" / "notes.url"
        for _ in range(100):
            if url_file.exists():
                break
            await asyncio.sleep(0.02)
        assert url_file.exists(), "notes.url should be written by _publish_url"

        url = url_file.read_text().strip()
        assert url == controller.server.url
        # The page is actually serveable at the published URL (same path the app
        # probes before raising the window).
        async with httpx.AsyncClient() as client:
            resp = await client.get(url + "/")
        assert resp.status_code == 200
        assert "Sonar Notes" in resp.text
    finally:
        stop.set()
        served_url = await asyncio.wait_for(task, timeout=5)

    assert served_url == url
    # aclose() ran in serve()'s finally: the server is torn down.
    assert controller.server._server is None
    # SONAR_NOTES_OPEN=0 means no browser tab was ever spawned; the file remains
    # for the FSEvents watcher.
    assert url_file.exists()
