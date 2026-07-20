"""Standalone Notes-session launcher — `python -m notes`.

Stands up the Sonar Notes UI (:8771) plus the full review/summarize/save/discard
path WITHOUT the voice stack — no mic, STT, TTS, or VAD is imported. The native
macOS app (NotesBackend) spawns this only when the voice loop isn't already
serving :8771, so a user can start a Notes session from the menu bar alone.

It reuses ``NotesController`` verbatim (no edits to controller.py / server.py):
``start()`` binds the port, serves ui.html, and — via the same ``_publish_url``
path the voice loop uses — writes ``~/.sonar/run/notes.url``, which is the app's
single "raise the Notes window" signal. With nothing feeding ``feed()``, the
stub transcribe is never invoked and the session records an empty transcript;
this proves the window -> WKWebView -> :8771 -> ops/save flow end to end, not
live diarization (that still needs the voice stack).

Env seams mirror the daemons (see scripts/sonar.sh): ``SONAR_NOTES_PORT`` (8771),
``SONAR_VAULT_PATH``, ``SONAR_OLLAMA_URL``, ``SONAR_HOME``. The app injects
``SONAR_NOTES_OPEN=0`` so ``_publish_url`` still writes notes.url (for the app's
FSEvents watcher) while the ``open <url>`` shell-out is skipped — the native
NSWindow loads the page and no browser tab races it. Because this leaves
``open_browser`` at its default (True), ``_publish_url`` always runs; passing
``open_browser=False`` would return before it and blind the watcher.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

from notes.controller import NotesController

log = logging.getLogger("sonar.notes.main")

_DEFAULT_NOTES_PORT = 8771
_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_DEFAULT_VAULT_PATH = "~/Documents/Obsidian Vault"


class _NullEmbedder:
    """No-op speaker embedder for the mic-less standalone session.

    The real ``EcapaEmbedder`` downloads ~80 MB (torch + speechbrain) on first
    ``load()``. A UI-only session never diarizes — nothing feeds ``feed()`` — so
    swapping in this stub keeps the launcher hermetic and instant and avoids a
    misleading "diarization degraded" banner. ``embed()`` / ``embed_windows()``
    are never called with no frames, but return the same "no signal" values the
    real embedder degrades to, just in case.
    """

    async def load(self) -> None:
        return None

    async def embed(self, _pcm: bytes) -> None:
        return None

    async def embed_windows(self, _pcm: bytes) -> list:
        return []

    async def aclose(self) -> None:
        return None


async def _stub_transcribe(_pcm: bytes) -> str:
    """No STT in the standalone session — nothing feeds the mic path."""
    return ""


def _notes_port() -> int:
    raw = os.environ.get("SONAR_NOTES_PORT", str(_DEFAULT_NOTES_PORT))
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid SONAR_NOTES_PORT=%r — using %d", raw, _DEFAULT_NOTES_PORT)
        return _DEFAULT_NOTES_PORT


def _vault_path() -> Path:
    return Path(
        os.environ.get("SONAR_VAULT_PATH", _DEFAULT_VAULT_PATH)
    ).expanduser()


def _ollama_url() -> str:
    return os.environ.get("SONAR_OLLAMA_URL", _DEFAULT_OLLAMA_URL)


def build_controller() -> NotesController:
    """A NotesController for a mic-less standalone session, reading the same
    SONAR_* env the daemons use. ``open_browser`` is left at its default (True)
    so ``_publish_url`` always writes notes.url; the app gates the actual browser
    open with SONAR_NOTES_OPEN=0. A null embedder keeps the launcher offline."""
    return NotesController(
        transcribe=_stub_transcribe,
        vault_path=_vault_path(),
        ollama_url=_ollama_url(),
        port=_notes_port(),
        embedder=_NullEmbedder(),
    )


async def serve(controller: NotesController, stop: asyncio.Event) -> str:
    """Start the session, block until ``stop`` is set, then tear everything down.

    Returns the served URL (handy for tests). ``aclose()`` runs in a ``finally``
    so a cancel/exception on the wait still stops the server and worker.
    """
    url = await controller.start()
    log.info("Sonar Notes standalone session on %s — Ctrl-C to stop", url)
    try:
        await stop.wait()
    finally:
        await controller.aclose()
    return url


async def _main_async() -> None:
    controller = build_controller()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # add_signal_handler is unavailable on some platforms; on macOS it works
        # and gives a clean async shutdown. asyncio.run() also raises
        # KeyboardInterrupt on SIGINT, which main() swallows as a backstop.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await serve(controller, stop)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SONAR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main_async())


if __name__ == "__main__":
    main()
