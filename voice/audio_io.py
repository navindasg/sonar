"""Local audio output for the voice loop — a duckable, flushable PCM player.

osvoice's Pipeline pushed TTS frames onto a queue for a *browser* to play (and
the browser did echo cancellation for free). Playing on the same Mac instead, we
own playback here: a callback-mode sounddevice output stream fed from a byte
buffer, with two hooks the full-duplex loop needs —

  * ``set_gain`` so a suspected barge-in can *duck* the reply (raising the mic
    SNR to confirm), and
  * ``flush`` so a confirmed barge-in silences the reply in one buffer (<~40 ms).

``last_rms`` reports the level actually on the speaker (post-gain) so
``EchoGate`` can threshold barge-in against real-time echo. Kokoro emits PCM16
mono @24 kHz; that is the only format this plays.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Final

import numpy as np

log = logging.getLogger("sonar.voice.audio")

_INT16_MAX: Final = 32767
_INT16_MIN: Final = -32768


def rms_pcm16(frame: bytes) -> float:
    """RMS energy of a PCM16 mono frame mapped to ~0..1 (empty -> 0)."""
    a = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
    if a.size == 0:
        return 0.0
    return min(1.0, float(np.sqrt(np.mean(a * a))))


def _rms_i16(samples: np.ndarray) -> float:
    """RMS of an int16 sample array mapped to ~0..1."""
    if samples.size == 0:
        return 0.0
    a = samples.astype(np.float32) / 32768.0
    return min(1.0, float(np.sqrt(np.mean(a * a))))


class OutputPlayer:
    """Callback-fed PCM16 speaker with duck (gain) and flush (drop) controls.

    All buffer access is under one lock shared with the PortAudio callback
    thread; the callback only ever does bounded, allocation-light work.
    """

    def __init__(self, samplerate: int = 24_000, blocksize: int = 1024) -> None:
        self._sr = samplerate
        self._blocksize = blocksize
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._gain = 1.0
        self._last_rms = 0.0
        self._stream: Any | None = None

    def start(self) -> None:
        """Open and start the output stream (lazy sounddevice import)."""
        import sounddevice as sd  # lazy: keep module import cheap/hardware-free

        self._stream = sd.RawOutputStream(
            samplerate=self._sr,
            channels=1,
            dtype="int16",
            blocksize=self._blocksize,
            callback=self._callback,
        )
        self._stream.start()
        log.info("output player started @%d Hz", self._sr)

    def _callback(self, outdata, frames, _time, status) -> None:  # PortAudio thread
        if status:
            log.debug("output status: %s", status)
        need = frames * 2  # int16 mono
        with self._lock:
            take = bytes(self._buf[:need])
            del self._buf[:need]
            gain = self._gain
        if len(take) < need:  # underrun -> pad with silence, never block
            take = take + b"\x00" * (need - len(take))
        samples = np.frombuffer(take, dtype="<i2")
        if gain != 1.0:
            samples = np.clip(
                samples.astype(np.float32) * gain, _INT16_MIN, _INT16_MAX
            ).astype("<i2")
        self._last_rms = _rms_i16(samples)
        outdata[:] = samples.tobytes()

    def write(self, pcm: bytes) -> None:
        """Enqueue synthesized PCM16 bytes for playback (FIFO)."""
        if not pcm:
            return
        with self._lock:
            self._buf.extend(pcm)

    def flush(self) -> None:
        """Drop all queued audio immediately (barge-in silence)."""
        with self._lock:
            self._buf.clear()

    def set_gain(self, gain: float) -> None:
        """Set output gain in [0, 1] (1.0 = full, lower = ducked)."""
        self._gain = max(0.0, min(1.0, gain))

    @property
    def gain(self) -> float:
        return self._gain

    def pending_bytes(self) -> int:
        """How many PCM bytes are still queued (0 => reply finished playing)."""
        with self._lock:
            return len(self._buf)

    def last_rms(self) -> float:
        """RMS of the most recent frame put on the speaker, post-gain (~0..1)."""
        return self._last_rms

    def stop(self) -> None:
        """Stop and close the stream; safe to call more than once."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                log.debug("error closing output stream", exc_info=True)
