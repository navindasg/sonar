"""Notes session controller — the pipeline glue voice_loop drives.

Owns one session at a time: mic frames (already VAD-scored by the voice loop)
come in through `feed()`, endpointed utterances flow through a single worker
(transcribe -> speaker-embed -> diarize -> segment), and every state change is
broadcast to the browser UI via NotesServer. Client ops from the UI land in
`apply_client_op`. All session state lives in one immutable SessionState
(session.py); this class only sequences IO around it.

Lifecycle: `start()` prepares everything (server, browser tab, embedder) with
feeding OFF so the voice loop can speak its ack without the mic hearing it into
the transcript; `begin_capture()` then opens the tap. `end()` — from the UI
End button or a spoken stop-phrase — drains in-flight utterances, gets the AI
overview, and hands the session to the UI for review/save.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from osvoice.vad import Endpointer, VadEvent

from notes import session as sess
from notes import store
from notes.diarize import SpeakerRegistry
from notes.embed import EcapaEmbedder
from notes.intent import wants_notes_stop
from notes.server import NotesServer
from notes.summarize import DEFAULT_MODEL, summarize

log = logging.getLogger("sonar.notes")

_FRAME_S = 512 / 16_000            # one Silero frame = 32 ms
_PREROLL_FRAMES = 8                # ~256 ms lead-in, same as the voice loop
_PARTIAL_FRAMES = 24               # live partial every ~768 ms (transcribe is shared with MLX)
_MIN_UTTER_FRAMES = 7              # <~224 ms: too short for parakeet to say anything

Transcribe = Callable[[bytes], Awaitable[str]]


class NotesController:
    def __init__(
        self,
        *,
        transcribe: Transcribe,
        vault_path: Path | str,
        ollama_url: str = "http://127.0.0.1:11434",
        model: str | None = None,
        host: str = "127.0.0.1",
        port: int = 8771,
        embedder: EcapaEmbedder | None = None,
        open_browser: bool = True,
        silence_ms: int | None = None,
        sim_threshold: float | None = None,
        on_ended: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._transcribe = transcribe
        self._vault = Path(vault_path)
        self._ollama_url = ollama_url.rstrip("/")
        self._model = model or os.environ.get("SONAR_NOTES_MODEL", DEFAULT_MODEL)
        self._embedder = embedder if embedder is not None else EcapaEmbedder()
        self._embed_ok = False
        self._open_browser = open_browser
        self._silence_ms = silence_ms or int(os.environ.get("SONAR_NOTES_SILENCE_MS", "700"))
        self._threshold = sim_threshold or float(os.environ.get("SONAR_NOTES_SIM_THRESHOLD", "0.40"))
        self.on_ended = on_ended
        self.server = NotesServer(self, host=host, port=port)

        self.state: sess.SessionState | None = None
        self._feeding = False
        self._ending = False
        self._on_ended_fired = False
        self._save_path: Path | None = None
        self._registry = SpeakerRegistry(threshold=self._threshold)
        self._endpointer = Endpointer(silence_ms=self._silence_ms)
        self._preroll: deque[bytes] = deque(maxlen=_PREROLL_FRAMES)
        self._utterance: list[bytes] = []
        self._capturing = False
        self._since_partial = 0
        self._frames_fed = 0
        self._utter_t0 = 0.0
        self._queue: asyncio.Queue[tuple[bytes, float, float]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._partial_task: asyncio.Task[None] | None = None

    # ---- lifecycle -----------------------------------------------------
    @property
    def recording(self) -> bool:
        """True while a session exists and hasn't been ended."""
        return self.state is not None and self.state.status == sess.RECORDING

    @property
    def wants_frames(self) -> bool:
        """True while mic frames should be routed to the notes pipeline."""
        return self._feeding

    @property
    def active(self) -> bool:
        """True while a notes session owns the mic — RECORDING or SUMMARIZING.

        The voice loop keeps mic frames out of the assistant path until the
        session is handed back (on_ended, at REVIEW/DISCARDED): during the
        summarize gap the mic is still hot but must not spawn a harness turn.
        """
        return self.state is not None and self.state.status in (
            sess.RECORDING, sess.SUMMARIZING
        )

    async def start(self, title_hint: str | None = None, now: datetime | None = None) -> str:
        """Prepare a fresh session (feeding stays OFF; see begin_capture)."""
        now = now or datetime.now()
        title = title_hint or f"Notes {now.strftime('%Y-%m-%d %H-%M')}"
        self.state = sess.SessionState(title=title, started_at=now.isoformat(timespec="seconds"))
        self._save_path = None
        self._feeding = False
        self._ending = False
        self._on_ended_fired = False
        self._registry = SpeakerRegistry(threshold=self._threshold)
        self._endpointer = Endpointer(silence_ms=self._silence_ms)
        self._preroll.clear()
        self._utterance = []
        self._capturing = False
        self._since_partial = 0
        self._frames_fed = 0
        # This controller is long-lived and reused for every "take notes". A
        # worker from a prior session is still parked on the OLD queue's
        # `await get()`; cancel and await it BEFORE rebinding, then start a fresh
        # worker on the new queue — otherwise session 2's feed() enqueues to a
        # queue nothing consumes and end()'s queue.join() would hang forever.
        await self._stop_worker()
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._work())

        await self.server.start()
        self._launch_browser()
        try:
            await self._embedder.load()
            self._embed_ok = True
        except Exception as exc:  # noqa: BLE001 — notes still work, single-speaker
            log.warning("speaker embedder unavailable (%s) — diarization disabled", exc)
            self._embed_ok = False
        if not self._embed_ok:
            # Surface the one-speaker degradation to the UI (see the shared
            # `diarization_degraded` flag) instead of failing silently.
            self.state = sess.set_diarization_degraded(self.state, True)
        await self._broadcast_state()
        return self.server.url

    async def _stop_worker(self) -> None:
        """Cancel and await the utterance worker (idempotent)."""
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    def begin_capture(self) -> None:
        """Open the mic tap (called after the spoken ack has fully played)."""
        if self.recording:
            self._feeding = True

    # ---- mic path (called from the voice loop's frame loop) ------------
    def feed(self, frame: bytes, vad_prob: float) -> None:
        """One 32 ms frame + its Silero probability. Sync; schedules async work."""
        if not self._feeding:
            return
        self._frames_fed += 1
        if self._capturing:
            self._utterance.append(frame)
            self._since_partial += 1
            if self._since_partial >= _PARTIAL_FRAMES:
                self._since_partial = 0
                self._schedule_partial()
        else:
            self._preroll.append(frame)

        ev = self._endpointer.update(vad_prob)
        if ev == VadEvent.SPEECH_START:
            self._capturing = True
            self._utterance = list(self._preroll)
            self._since_partial = 0
            self._utter_t0 = (self._frames_fed - len(self._utterance)) * _FRAME_S
        elif ev == VadEvent.TURN_END:
            self._capturing = False
            turn, self._utterance = self._utterance, []
            self._preroll.clear()  # don't let this turn's tail leak into the next
            self._cancel_partial()
            if len(turn) >= _MIN_UTTER_FRAMES:
                # The endpointer only fires after silence_ms of quiet, so the
                # speech actually ENDED that long ago — don't count it into t1.
                t1 = max(
                    self._utter_t0 + _FRAME_S,
                    self._frames_fed * _FRAME_S - self._silence_ms / 1000.0,
                )
                self._queue.put_nowait((b"".join(turn), self._utter_t0, t1))

    def _schedule_partial(self) -> None:
        if self._partial_task is not None and not self._partial_task.done():
            return  # previous partial still transcribing; skip this tick
        pcm = b"".join(self._utterance)
        self._partial_task = asyncio.create_task(self._emit_partial(pcm))

    async def _emit_partial(self, pcm: bytes) -> None:
        try:
            text = await self._transcribe(pcm)
        except Exception as exc:  # noqa: BLE001 — partials are throwaway; the final utterance toasts on real failure
            log.debug("notes partial transcription failed: %s", exc)
            return
        if text.strip() and self._feeding:
            await self.server.broadcast({"type": "partial", "text": text})

    def _cancel_partial(self) -> None:
        if self._partial_task is not None and not self._partial_task.done():
            self._partial_task.cancel()
        self._partial_task = None

    async def _work(self) -> None:
        """Single consumer: keeps segments ordered even when STT is slow."""
        while True:
            pcm, t0, t1 = await self._queue.get()
            try:
                await self._handle_utterance(pcm, t0, t1)
            except Exception:  # noqa: BLE001 — one bad utterance must not kill notes
                log.exception("notes utterance failed")
            finally:
                self._queue.task_done()

    async def _handle_utterance(self, pcm: bytes, t0: float, t1: float) -> None:
        if self.state is None or self.state.status != sess.RECORDING:
            return
        try:
            text = await self._transcribe(pcm)
        except Exception as exc:  # noqa: BLE001 — a failed STT must not silently drop a line
            # This utterance had real audio (it cleared the min-frames gate), so
            # an exception here means transcription FAILED, not that the room was
            # quiet — say so rather than dropping the line invisibly.
            log.warning("notes transcription failed: %s", exc)
            await self.server.broadcast(
                {"type": "error", "message": "a line couldn't be transcribed"}
            )
            return
        if not text.strip():
            return
        if wants_notes_stop(text):
            # The stop command itself never enters the transcript.
            asyncio.create_task(self.end())
            return
        emb = await self._embedder.embed(pcm) if self._embed_ok else None
        assignment = self._registry.assign(emb, duration_s=t1 - t0)
        self.state = sess.add_segment(self.state, assignment.speaker, text, t0, t1)
        await self.server.broadcast({"type": "partial", "text": ""})
        await self._broadcast_state()

    # ---- ending / saving ------------------------------------------------
    async def end(self) -> None:
        """Stop capture, drain in-flight utterances, produce the AI overview."""
        if self.state is None or self.state.status != sess.RECORDING or self._ending:
            return
        self._ending = True
        self._feeding = False
        self._cancel_partial()
        # Drain BEFORE flipping status: queued utterances only land while the
        # session still reads as recording (see _handle_utterance's guard).
        await self._queue.join()
        # A Discard can land during the drain — the status still reads RECORDING
        # so the UI still offered it. That terminal state wins; don't resurrect
        # a discarded session into SUMMARIZING/REVIEW.
        if self.state.status != sess.RECORDING:
            self._ending = False
            return
        self.state = sess.set_status(self.state, sess.SUMMARIZING)
        await self._broadcast_state()
        summary = await self._summarize()
        # Discard can also land while we awaited the (slow) summary; re-check
        # before forcing REVIEW so a DISCARDED session is never clobbered.
        if self.state.status != sess.SUMMARIZING:
            self._ending = False
            return
        self.state = sess.set_status(sess.set_summary(self.state, summary), sess.REVIEW)
        self._ending = False
        await self._broadcast_state()
        await self._fire_on_ended()

    async def _fire_on_ended(self) -> None:
        """Hand the mic back to the voice loop — at most once per session exit."""
        if self._on_ended_fired or self.on_ended is None:
            return
        self._on_ended_fired = True
        with contextlib.suppress(Exception):
            await self.on_ended()

    async def _summarize(self) -> str:
        import httpx

        async with httpx.AsyncClient(base_url=self._ollama_url) as client:
            return await summarize(client, self.state, self._model)

    async def save(self) -> None:
        if self.state is None or self.state.status not in (sess.REVIEW, sess.SAVED):
            return
        try:
            target = await asyncio.to_thread(
                store.save_note, self.state, self._vault, datetime.now(), self._save_path
            )
        except OSError as exc:
            log.error("saving notes failed: %s", exc)
            await self.server.broadcast({"type": "error", "message": f"save failed: {exc}"})
            return
        self._save_path = target
        rel = target.relative_to(self._vault).as_posix()
        self.state = sess.mark_saved(self.state, rel)
        await self._broadcast_state()

    async def discard(self) -> None:
        if self.state is None or self.state.status == sess.DISCARDED:
            return
        # on_ended matters whenever we leave an ACTIVE session (recording, or
        # racing end() through its drain/summary): that's when the voice loop
        # still needs the mic back. From REVIEW/SAVED the mic was already handed
        # back, so the once-guard in _fire_on_ended makes that a harmless no-op.
        was_active = self.state.status in (sess.RECORDING, sess.SUMMARIZING)
        self._feeding = False
        self._cancel_partial()
        self.state = sess.set_status(self.state, sess.DISCARDED)
        await self._broadcast_state()
        if was_active:
            await self._fire_on_ended()

    # ---- server-facing (NotesOps) ---------------------------------------
    def state_json(self) -> dict:
        if self.state is None:
            return {"type": "state", "status": "idle", "rev": -1,
                    "segments": [], "speakers": [], "summary": "", "title": "",
                    "saved_path": "", "elapsed_s": 0.0, "started_at": "",
                    "diarization_degraded": False}
        return sess.to_json(self.state, elapsed_s=self._frames_fed * _FRAME_S)

    async def apply_client_op(self, msg: dict) -> None:
        """One edit/action from the browser; unknown or invalid ops are no-ops."""
        if self.state is None:
            return
        op = msg.get("op")
        if op == "end":
            await self.end()
            return
        if op == "save":
            await self.save()
            return
        if op == "discard":
            await self.discard()
            return
        before = self.state
        if op == "rename":
            self.state = sess.rename_speaker(before, msg.get("speaker"), msg.get("name"))
        elif op == "edit_segment":
            self.state = sess.edit_segment_text(before, msg.get("id"), msg.get("text"))
        elif op == "delete_segment":
            self.state = sess.delete_segment(before, msg.get("id"))
        elif op == "reassign":
            self.state = sess.reassign_segment(before, msg.get("id"), msg.get("speaker"))
        elif op == "add_speaker":
            self.state = sess.add_speaker(before)
        elif op == "set_title":
            self.state = sess.set_title(before, msg.get("title"))
        elif op == "edit_summary":
            self.state = sess.set_summary(before, msg.get("markdown"))
        if self.state is not before:
            await self._broadcast_state()

    async def _broadcast_state(self) -> None:
        await self.server.broadcast(self.state_json())

    def _launch_browser(self) -> None:
        if not self._open_browser:
            return
        url = self.server.url
        # Record the URL somewhere durable BEFORE trying to open a tab: `open`
        # is best-effort (suppressed) and a daemon's stdout is easily lost, so a
        # failed open would otherwise strand a captured meeting with no way to
        # reach Save (which lives only in the browser page).
        self._publish_url(url)
        if os.environ.get("SONAR_NOTES_OPEN", "1") == "0" or sys.platform != "darwin":
            return
        with contextlib.suppress(Exception):
            subprocess.Popen(["open", url])

    def _publish_url(self, url: str) -> None:
        """Write the notes UI URL to a stable file and log it prominently, so a
        user can always reach the page even when no browser tab appears."""
        log.info("notes UI ready — open %s to review and save", url)
        home = Path(os.environ.get("SONAR_HOME", os.path.expanduser("~/.sonar")))
        with contextlib.suppress(OSError):
            run_dir = home / "run"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "notes.url").write_text(url + "\n", encoding="utf-8")

    async def aclose(self) -> None:
        self._feeding = False
        self._cancel_partial()
        await self._stop_worker()
        await self.server.stop()
