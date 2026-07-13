# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "mlx>=0.18",
#   "mlx-lm>=0.18",
#   "mlx-audio[tts]==0.4.5",   # Kokoro TTS backend (0.4.5 fixes a vocoder broadcast crash on ~half of sentences)
#   "misaki[en]>=0.7",         # Kokoro G2P (English)
#   "parakeet-mlx==0.5.2",     # STT
#   "numba>=0.59",             # pin: parakeet->librosa pulls an ancient numba otherwise
#   "silero-vad==6.2.1",
#   "torch",
#   "numpy>=1.26",
#   "sounddevice>=0.4",        # mic in + speaker out (not in osvoice pyproject)
#   "websockets>=13",
#   "httpx>=0.27",
# ]
# ///
"""Sonar voice loop (I0) — press F5, speak, hear a vault-grounded answer.

This is the integration that closes the loop the two spikes left open:
``voice/stt_bridge.py`` proved mic -> STT -> box, and ``overlay/bridge.py``
proved typed -> harness -> box. This joins them and adds the missing edge —
answer -> TTS -> speaker — so one F5 press runs the whole turn:

    F5/start  -> mic on, live partials fill the box
    turn end  -> final transcript -> harness /v1 (tool loop, grounded answer)
    answer    -> box text  AND  Kokoro TTS -> speaker
    (slow tool turns get a short spoken ack up front so there's no dead air)

Full-duplex barge-in: the mic stays hot while Sonar speaks, and ``EchoGate``
tells the reply's own echo apart from you talking over it (ducking the output to
confirm, then cancelling reply + audio on a real interruption). Typing in the
box still works and now also gets a spoken answer.

It is the single WebSocket SERVER the overlay (glow ``init.lua``) connects to on
:8770 — run this INSTEAD of ``overlay/bridge.py`` (they share the port). The
harness must be up separately on :8787.

Wire protocol (this = SERVER; the glow init.lua = CLIENT):
  <- {"cmd": "start"|"stop"}       mic on / off  (also glow show/hide)
  <- {"text": "<question>"}        typed question -> same harness+TTS turn
  -> {"transcript": "...", "partial": bool}   live / final STT into the box
  -> {"turn": "start"|"end"}                  bracket a turn (overlay busy)
  -> {"state": "...", "level": n}             glow modulation
  -> {"step": {...}}                          one harness step-event
  -> {"answer": "<delta>", "partial": bool}   streamed answer text

Run:  cd voice && SONAR_HARNESS_URL=http://127.0.0.1:8787 uv run voice_loop.py
(or:  scripts/sonar.sh voice)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import sys
from collections import deque
from typing import Any, AsyncIterator

import numpy as np
import sounddevice as sd
import websockets

# Vendored osvoice adapters (this file lives next to the osvoice package).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from osvoice.aggregator import aggregate  # noqa: E402
from osvoice.providers.parakeet import ParakeetMLX  # noqa: E402
from osvoice.providers.tts_kokoro import KokoroTTS  # noqa: E402
from osvoice.vad import Endpointer, VadEvent  # noqa: E402

from audio_io import OutputPlayer, rms_pcm16  # noqa: E402
from echo_gate import EchoGate  # noqa: E402
from acks import next_ack  # noqa: E402
from harness_client import DELTA, STEP, stream_turn  # noqa: E402
from history import append_turn  # noqa: E402

log = logging.getLogger("sonar.voice")

HOST = os.environ.get("SONAR_GLOW_HOST", "127.0.0.1")
PORT = int(os.environ.get("SONAR_GLOW_PORT", "8770"))
HARNESS_URL = os.environ.get("SONAR_HARNESS_URL", "http://127.0.0.1:8787").rstrip("/")

SAMPLE_RATE = 16_000
FRAME = 512                          # Silero window @16 kHz (32 ms)
BYTES_PER_FRAME = FRAME * 2          # PCM16 mono
OUTPUT_SR = 24_000                   # Kokoro output rate
PARTIAL_FRAMES = 12                  # re-transcribe + push a partial every ~384 ms
MIN_UTTER_SAMPLES = SAMPLE_RATE // 5  # ignore <200 ms slivers (too short for parakeet)
PREROLL_FRAMES = 8                   # ~256 ms of pre-speech kept for lead-in

# Short spoken ack the instant a turn starts, to cover the harness's blocking
# tool loop (~8 s on tool turns) so voice turns never open with dead air. Rotated
# per turn (see acks.py) so it isn't the same phrase every time; set SONAR_VOICE_ACK
# to force one fixed phrase instead.
ACK_TEXT = os.environ.get("SONAR_VOICE_ACK", "").strip()
DUCK_GAIN = float(os.environ.get("SONAR_VOICE_DUCK_GAIN", "0.35"))

# Conversation memory within one F5 session: prior turns ride along so follow-ups
# ("what about next week?") resolve against context. Bounded by a rolling token
# budget (~chars/4) so it never grows unbounded — oldest turns drop first. Reset
# when the overlay opens a fresh session. ~4k tokens ≈ 10-15 short voice turns.
HISTORY_TOKEN_BUDGET = int(os.environ.get("SONAR_VOICE_HISTORY_TOKENS", "4000"))


async def _one(clause: str) -> AsyncIterator[str]:
    """Wrap a single clause as an async iterator for the TTS provider."""
    yield clause


class VoiceLoop:
    """One overlay connection's full-duplex voice turn-runner."""

    def __init__(self) -> None:
        self.stt = ParakeetMLX()
        self.tts = KokoroTTS()
        self.player = OutputPlayer(samplerate=OUTPUT_SR)
        self.endpointer = Endpointer(silence_ms=600)
        self.gate = EchoGate()
        self.silero: Any | None = None
        self._torch: Any | None = None
        self.harness: Any | None = None  # httpx.AsyncClient
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mic: sd.RawInputStream | None = None
        self.frames: asyncio.Queue[bytes] | None = None
        self.listening = False
        self.speaking = False
        self._response_task: asyncio.Task[None] | None = None
        self.history: list[dict[str, str]] = []  # session memory (bounded)
        self._ack_rng = random.Random()          # rotate acks; avoid back-to-back repeats
        self._last_ack: str | None = None

    async def load(self) -> None:
        print("[voice] loading parakeet STT (first run downloads ~1-2GB)…", flush=True)
        await self.stt.load()
        print("[voice] loading kokoro TTS…", flush=True)
        await self.tts.load()
        print("[voice] loading silero VAD…", flush=True)
        import torch
        from silero_vad import load_silero_vad

        torch.set_num_threads(1)
        self._torch = torch
        self.silero = load_silero_vad()
        self.silero(torch.zeros(FRAME), SAMPLE_RATE)  # warmup
        self.silero.reset_states()

        self.player.start()
        import httpx

        self.harness = httpx.AsyncClient(base_url=HARNESS_URL)
        print(f"[voice] ready — harness {HARNESS_URL}", flush=True)

    async def aclose(self) -> None:
        await self._cancel_response()
        self.stop_mic()
        self.player.stop()
        if self.harness is not None:
            await self.harness.aclose()

    # ---- mic capture (PortAudio callback thread -> asyncio queue) ----
    def _on_audio(self, indata, _frames, _time, status) -> None:
        if status:
            print(f"[voice] audio status: {status}", flush=True)
        if self.loop and self.frames is not None:
            self.loop.call_soon_threadsafe(self.frames.put_nowait, bytes(indata))

    def start_mic(self) -> None:
        if self.mic is not None:
            return
        self.frames = asyncio.Queue()
        self.mic = sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=FRAME, callback=self._on_audio,
        )
        self.mic.start()

    def stop_mic(self) -> None:
        if self.mic is not None:
            with contextlib.suppress(Exception):
                self.mic.stop()
                self.mic.close()
            self.mic = None
        self.frames = None
        self.endpointer.reset()
        if self.silero is not None:
            self.silero.reset_states()

    def _silero_prob(self, frame: bytes) -> float:
        a = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
        t = self._torch.from_numpy(np.ascontiguousarray(a)).float()
        return float(self.silero(t, SAMPLE_RATE).item())

    # ---- connection handling ----
    async def handler(self, ws) -> None:
        print("[voice] overlay connected", flush=True)
        self.loop = asyncio.get_running_loop()
        consumer = asyncio.create_task(self._consume(ws))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                text = msg.get("text")
                cmd = msg.get("cmd")
                if cmd == "say" and isinstance(text, str) and text.strip():
                    # Proactive push (e.g. the scheduled morning brief): speak the
                    # given text directly — NO harness turn. Checked before the
                    # typed path so its 'text' isn't treated as a question.
                    self._start_say(ws, text.strip())
                elif isinstance(text, str) and text.strip():
                    self._start_response(ws, text.strip())  # typed -> harness + TTS
                elif cmd == "start":
                    self.listening = True
                    self.history = []  # fresh conversation each time the overlay opens
                    self.start_mic()
                    await self._send(ws, {"state": "listening", "level": 0.2})
                    print("[voice] listening", flush=True)
                elif cmd == "stop":
                    # Second F5 / overlay close: stop EVERYTHING now — mic off,
                    # turn cancelled, and any audio still queued is flushed so the
                    # reply doesn't keep playing after you've dismissed it.
                    self.listening = False
                    self.stop_mic()
                    await self._silence()
                    await self._send(ws, {"state": "idle", "level": 0.0})
                    print("[voice] stopped", flush=True)
        finally:
            consumer.cancel()
            self.listening = False
            self.stop_mic()
            await self._silence()  # dropped socket: don't keep talking to a gone overlay

    async def _consume(self, ws) -> None:
        """Single mic loop: capture+STT while listening, barge-in while speaking."""
        buf = bytearray()
        preroll: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        utterance: list[bytes] = []
        capturing = False
        ticks = 0
        since_partial = 0
        while True:
            if not self.listening or self.frames is None:
                capturing = False
                utterance.clear()
                preroll.clear()
                await asyncio.sleep(0.05)
                continue
            try:
                chunk = await asyncio.wait_for(self.frames.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            buf.extend(chunk)
            while len(buf) >= BYTES_PER_FRAME:
                frame = bytes(buf[:BYTES_PER_FRAME])
                del buf[:BYTES_PER_FRAME]
                ticks += 1
                vad = self._silero_prob(frame)
                level = rms_pcm16(frame)

                if self.speaking:
                    if await self._barge_check(ws, vad, level, preroll):
                        # barged: fall through and let the capture path see this
                        # frame as the start of the new utterance.
                        capturing = False
                        utterance = []
                        since_partial = 0
                    else:
                        preroll.append(frame)
                        continue

                if ticks % 3 == 0:
                    await self._send(ws, {
                        "state": "thinking" if capturing else "listening",
                        "level": level,
                    })
                if capturing:
                    utterance.append(frame)
                    since_partial += 1
                    if since_partial >= PARTIAL_FRAMES:
                        since_partial = 0
                        await self._emit_transcript(ws, utterance, final=False)
                else:
                    preroll.append(frame)

                ev = self.endpointer.update(vad)
                if ev == VadEvent.SPEECH_START:
                    capturing = True
                    utterance = list(preroll)
                    since_partial = 0
                elif ev == VadEvent.TURN_END:
                    capturing = False
                    turn, utterance = utterance, []
                    text = await self._emit_transcript(ws, turn, final=True)
                    if text.strip():
                        self._start_response(ws, text.strip())

    async def _barge_check(
        self, ws, vad: float, level: float, preroll: deque[bytes]
    ) -> bool:
        """While speaking, decide duck/barge-in. Return True iff we barged in.

        Ducking is TRANSIENT: we lower the output only while a suspect run is
        building, and restore full volume the moment it clears — so a lone echo
        transient never quiets the rest of the reply.
        """
        decision = self.gate.observe(vad, level, self.player.last_rms())
        if decision.barge_in:
            await self._barge_in(ws)
            return True
        if decision.duck:
            if self.player.gain > DUCK_GAIN:
                self.player.set_gain(DUCK_GAIN)
        elif self.player.gain < 1.0:
            self.player.set_gain(1.0)  # suspicion passed — un-duck
        return False

    async def _barge_in(self, ws) -> None:
        """User talked over the reply: kill reply+audio, reset, resume listening."""
        print("[voice] barge-in", flush=True)
        await self._silence()
        self.endpointer.reset()
        if self.silero is not None:
            self.silero.reset_states()
        await self._send(ws, {"answer": "", "partial": False})
        await self._send(ws, {"state": "listening", "level": 0.2})

    # ---- response: harness turn -> box + TTS ----
    def _start_response(self, ws, text: str) -> None:
        """(Re)start the response task for ``text``; a new turn replaces any old."""
        if self._response_task is not None and not self._response_task.done():
            self._response_task.cancel()
        self._response_task = asyncio.create_task(self._respond(ws, text))

    def _start_say(self, ws, text: str) -> None:
        """Start a proactive spoken message, replacing any in-flight turn."""
        if self._response_task is not None and not self._response_task.done():
            self._response_task.cancel()
        self._response_task = asyncio.create_task(self._speak_text(ws, text))

    async def _cancel_response(self) -> None:
        task = self._response_task
        self._response_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _silence(self) -> None:
        """Hard cutoff: kill the in-flight turn AND drop audio already queued.

        Order matters — cancel the response FIRST (so no more TTS frames can be
        written once the buffer is dropped), THEN flush the speaker. Cancelling
        alone only stops *feeding* new audio; whatever Kokoro already queued keeps
        playing. Also un-ducks and resets the echo gate so the next turn is clean.
        This is what makes a second F5 (or a barge-in) stop everything at once.
        """
        await self._cancel_response()
        self.player.flush()          # drop buffered PCM -> speaker goes quiet now
        self.player.set_gain(1.0)
        self.gate.reset()
        self.speaking = False

    async def _respond(self, ws, text: str) -> None:
        """Drive one turn: spoken ack -> streamed harness answer -> box + speaker."""
        self.speaking = True
        self.gate.reset()
        self.player.set_gain(1.0)
        await self._send(ws, {"turn": "start"})
        await self._send(ws, {"state": "thinking", "level": 0.6})
        # Prior turns ride along so follow-ups resolve against the session; the
        # completed turn is committed to history only on the clean path below (a
        # barged/cancelled turn is never remembered).
        messages = self.history + [{"role": "user", "content": text}]
        delta_q: asyncio.Queue[str | None] = asyncio.Queue()
        pump = asyncio.create_task(self._pump(ws, messages, delta_q))
        try:
            # Rotate the ack so it isn't "One sec." every turn (env forces a fixed one).
            ack = ACK_TEXT or next_ack(self._last_ack, self._ack_rng)
            self._last_ack = ack
            await self._speak_clause(ack)  # covers the blocking tool loop
            async for clause in aggregate(self._drain_deltas(delta_q)):
                await self._speak_clause(clause)
            answer = await pump
            await self._drain_playback()
            self._commit_history(text, answer)
        except asyncio.CancelledError:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            raise
        finally:
            await self._send(ws, {"answer": "", "partial": False})
            await self._send(ws, {"turn": "end"})
            await self._send(ws, {"state": "listening", "level": 0.2})
            self.speaking = False

    async def _speak_text(self, ws, text: str) -> None:
        """Speak already-composed text with NO harness turn (a proactive push, e.g.
        the scheduled morning brief). Displays the full text in the box (visible if
        it's open) and streams it to Kokoro clause by clause. Cancellable like a
        normal turn, so an F5 cutoff (``_silence``) silences it too.
        """
        self.speaking = True
        self.gate.reset()
        self.player.set_gain(1.0)
        await self._send(ws, {"turn": "start"})
        await self._send(ws, {"answer": text, "partial": True})
        try:
            async for clause in aggregate(self._text_chunks(text)):
                await self._speak_clause(clause)
            await self._drain_playback()
        finally:
            await self._send(ws, {"answer": "", "partial": False})
            await self._send(ws, {"turn": "end"})
            state = "listening" if self.listening else "idle"
            await self._send(ws, {"state": state, "level": 0.2 if self.listening else 0.0})
            self.speaking = False

    async def _text_chunks(self, text: str) -> AsyncIterator[str]:
        """Yield the whole text once so ``aggregate`` can split it into clauses."""
        yield text

    async def _pump(
        self, ws, messages: list[dict[str, str]], delta_q: asyncio.Queue[str | None]
    ) -> str:
        """Stream the harness turn: steps -> box, answer deltas -> box + delta_q.

        Returns the full accumulated answer text (empty on harness error) so the
        caller can commit it to the session's conversation memory.
        """
        parts: list[str] = []
        try:
            async for kind, val in stream_turn(self.harness, messages):
                if kind == STEP:
                    await self._send(ws, {"step": val})
                elif kind == DELTA:
                    parts.append(val)
                    await self._send(ws, {"answer": val, "partial": True})
                    await delta_q.put(val)
        except Exception as exc:  # noqa: BLE001 — surface harness failure, keep loop alive
            log.exception("harness turn failed")
            await self._send(ws, {"answer": f"[harness error: {exc}]", "partial": True})
        finally:
            await delta_q.put(None)  # sentinel: end of stream
        return "".join(parts)

    async def _drain_deltas(self, delta_q: asyncio.Queue[str | None]) -> AsyncIterator[str]:
        while True:
            val = await delta_q.get()
            if val is None:
                return
            yield val

    def _commit_history(self, user_text: str, answer: str) -> None:
        """Remember this turn (bounded) so follow-ups resolve against context."""
        self.history = append_turn(
            self.history, user_text, answer, HISTORY_TOKEN_BUDGET
        )

    async def _speak_clause(self, clause: str) -> None:
        """Synthesize one clause and queue its PCM frames for playback.

        A single clause that fails to synthesize (a TTS backend quirk) is logged
        and skipped, so one bad clause can never silence the rest of the reply.
        """
        text = clause.strip()
        if not text:
            return
        try:
            async for frame in self.tts.stream(_one(text)):
                self.player.write(frame)
        except Exception as exc:  # noqa: BLE001 — one bad clause must not kill the turn
            log.warning("TTS failed for clause %r: %s — skipping", text, exc)

    async def _drain_playback(self) -> None:
        """Wait until the speaker buffer empties so the reply is fully heard."""
        while self.player.pending_bytes() > 0:
            await asyncio.sleep(0.05)

    # ---- STT emit ----
    async def _emit_transcript(self, ws, chunks: list[bytes], final: bool) -> str:
        """Transcribe the audio-so-far, push it to the box, return the text.

        Feeds the WHOLE buffer to Parakeet as ONE chunk: a sub-window sliver
        underflows to a 2^64-4096 Metal alloc, and one-shot on the full buffer is
        the model's own warmup path (see stt_bridge.py for the root-cause note).
        """
        if not chunks:
            return ""
        pcm = b"".join(chunks)
        if np.frombuffer(pcm, dtype="<i2").size < MIN_UTTER_SAMPLES:
            return ""
        text = await self._transcribe_pcm(pcm)
        if text.strip():
            await self._send(ws, {"transcript": text, "partial": not final})
        return text

    async def _transcribe_pcm(self, pcm: bytes) -> str:
        async def audio() -> AsyncIterator[bytes]:
            yield pcm

        text = ""
        try:
            async for t in self.stt.stream(audio()):
                if t.is_final:
                    text = t.text
        except Exception as exc:  # noqa: BLE001 — one bad utterance must not kill the loop
            print(f"[voice] transcription error: {exc}", flush=True)
        return text

    async def _send(self, ws, msg: dict) -> None:
        """Best-effort JSON send; a dropped socket ends the connection loop, not us."""
        with contextlib.suppress(Exception):
            await ws.send(json.dumps(msg))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    loop = VoiceLoop()
    await loop.load()
    try:
        async with websockets.serve(loop.handler, HOST, PORT):
            print(f"[voice] serving ws://{HOST}:{PORT}; Ctrl-C to stop", flush=True)
            await asyncio.Future()
    finally:
        await loop.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[voice] bye", flush=True)
