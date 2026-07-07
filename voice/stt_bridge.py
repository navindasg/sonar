# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "mlx>=0.18",
#   "parakeet-mlx==0.5.2",
#   "numba>=0.59",           # pin: parakeet->librosa pulls an ancient numba (py<3.10) otherwise
#   "silero-vad==6.2.1",
#   "torch",
#   "numpy>=1.26",
#   "sounddevice>=0.4",
#   "websockets>=13",
# ]
# ///
"""Sonar STT bridge — mic -> Silero VAD endpointing -> Parakeet STT -> WebSocket.

A spike that gives the Hammerspoon overlay REAL speech-to-text without the full
osvoice pipeline. It reuses the vendored osvoice adapters:
  - osvoice.providers.parakeet.ParakeetMLX  (one-shot per-utterance transcription)
  - osvoice.vad.Endpointer                  (pure hysteresis endpointing)
and loads the Silero VAD model directly for per-frame speech probabilities.

Protocol (WebSocket SERVER on 127.0.0.1:8770; the glow init.lua is the CLIENT):
  <- {"cmd":"start"}     begin listening
  <- {"cmd":"stop"}      stop listening
  -> {"state": "...", "level": 0..1}   glow modulation while listening/thinking
  -> {"transcript": "..."}             one finished (endpointed) utterance

Run:  cd voice && uv run stt_bridge.py
First run downloads the parakeet + silero models and prompts for macOS Microphone
permission for the hosting terminal/process.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import deque

import numpy as np
import sounddevice as sd
import websockets

# Import the vendored osvoice adapters (this file lives next to the osvoice pkg).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from osvoice.providers.parakeet import ParakeetMLX  # noqa: E402
from osvoice.vad import Endpointer, VadEvent  # noqa: E402

HOST = os.environ.get("SONAR_GLOW_HOST", "127.0.0.1")
PORT = int(os.environ.get("SONAR_GLOW_PORT", "8770"))
SAMPLE_RATE = 16_000
FRAME = 512                 # Silero window @16 kHz (32 ms)
BYTES_PER_FRAME = FRAME * 2  # PCM16 mono
PARTIAL_FRAMES = 12         # re-transcribe + push a partial every ~384 ms while speaking
MIN_UTTER_SAMPLES = SAMPLE_RATE // 5  # ignore <200 ms slivers (too short for parakeet)


def rms_level(frame: bytes) -> float:
    """Map a PCM16 frame's RMS to ~0..1 so the glow breathes with the voice."""
    a = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
    if a.size == 0:
        return 0.0
    return max(0.0, min(1.0, float(np.sqrt(np.mean(a * a))) * 8.0))


class Bridge:
    def __init__(self) -> None:
        self.stt = ParakeetMLX()
        self.silero = None
        self._torch = None
        self.endpointer = Endpointer(silence_ms=600)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mic: sd.RawInputStream | None = None
        self.frames: asyncio.Queue[bytes] | None = None
        self.listening = False

    async def load(self) -> None:
        print("[stt] loading parakeet (first run downloads ~1-2GB)…", flush=True)
        await self.stt.load()
        print("[stt] loading silero…", flush=True)
        import torch
        from silero_vad import load_silero_vad

        torch.set_num_threads(1)
        self._torch = torch
        self.silero = load_silero_vad()
        self.silero(torch.zeros(FRAME), SAMPLE_RATE)  # warmup
        self.silero.reset_states()
        print("[stt] ready", flush=True)

    def _silero_prob(self, frame: bytes) -> float:
        a = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
        t = self._torch.from_numpy(np.ascontiguousarray(a)).float()
        return float(self.silero(t, SAMPLE_RATE).item())

    # ---- mic capture (PortAudio callback thread -> asyncio queue) ----
    def _on_audio(self, indata, _frames, _time, status) -> None:
        if status:
            print(f"[stt] audio status: {status}", flush=True)
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
            try:
                self.mic.stop()
                self.mic.close()
            except Exception:
                pass
            self.mic = None
        self.frames = None
        self.endpointer.reset()
        if self.silero is not None:
            self.silero.reset_states()

    async def handler(self, ws) -> None:
        print("[stt] glow connected", flush=True)
        self.loop = asyncio.get_running_loop()
        consumer = asyncio.create_task(self._consume(ws))
        try:
            async for msg in ws:
                try:
                    cmd = json.loads(msg).get("cmd")
                except Exception:
                    continue
                if cmd == "start":
                    self.listening = True
                    self.start_mic()
                    await ws.send(json.dumps({"state": "listening", "level": 0.2}))
                    print("[stt] listening", flush=True)
                elif cmd == "stop":
                    self.listening = False
                    self.stop_mic()
                    print("[stt] stopped", flush=True)
        finally:
            consumer.cancel()
            self.listening = False
            self.stop_mic()

    async def _consume(self, ws) -> None:
        buf = bytearray()
        preroll: deque[bytes] = deque(maxlen=8)  # ~256 ms of pre-speech
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
                if ticks % 3 == 0:
                    try:
                        await ws.send(json.dumps({"state": "thinking" if capturing else "listening",
                                                  "level": rms_level(frame)}))
                    except Exception:
                        return
                if capturing:
                    utterance.append(frame)
                    since_partial += 1
                    if since_partial >= PARTIAL_FRAMES:
                        since_partial = 0
                        await self._emit(ws, utterance, final=False)
                else:
                    preroll.append(frame)
                ev = self.endpointer.update(self._silero_prob(frame))
                if ev == VadEvent.SPEECH_START:
                    capturing = True
                    utterance = list(preroll)
                    since_partial = 0
                elif ev == VadEvent.TURN_END:
                    capturing = False
                    turn, utterance = utterance, []
                    await self._emit(ws, turn, final=True)

    async def _emit(self, ws, chunks: list[bytes], final: bool) -> None:
        """Transcribe the audio-so-far and push it to the box.

        Called repeatedly WHILE speaking (final=False, growing buffer) so the box
        fills chunk-by-chunk, then once on turn-end (final=True). Each call feeds
        the WHOLE buffer to Parakeet as a SINGLE chunk — parakeet-mlx crashes on a
        sub-window sliver (a ragged slice underflows to a 2^64-4096 Metal alloc),
        and one-shot on the full buffer is the model's own warmup path.
        """
        if not chunks:
            return
        pcm = b"".join(chunks)
        arr = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if arr.size < MIN_UTTER_SAMPLES:   # too short for parakeet to say anything
            return

        text = await self._run_stt(pcm)
        if final:
            peak = float(np.max(np.abs(arr)))
            print(f"[stt] final {arr.size / SAMPLE_RATE:.2f}s peak={peak:.3f} -> {text!r}", flush=True)
        elif text.strip():
            print(f"[stt] partial {arr.size / SAMPLE_RATE:.2f}s -> {text!r}", flush=True)
        try:
            if text.strip():
                await ws.send(json.dumps({"transcript": text, "partial": not final}))
            if final:
                await ws.send(json.dumps({"state": "listening", "level": 0.2}))
        except Exception:
            return

    async def _run_stt(self, pcm: bytes) -> str:
        """One-shot transcribe a PCM16 buffer via the vendored ParakeetMLX."""
        async def aiter():
            yield pcm

        text = ""
        try:
            async for t in self.stt.stream(aiter()):
                if t.is_final:
                    text = t.text
        except Exception as exc:
            print(f"[stt] transcription error: {exc}", flush=True)
        return text


async def main() -> None:
    bridge = Bridge()
    await bridge.load()
    async with websockets.serve(bridge.handler, HOST, PORT):
        print(f"[stt] serving ws://{HOST}:{PORT}; Ctrl-C to stop", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stt] bye", flush=True)
