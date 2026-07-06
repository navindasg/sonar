"""Silero VAD framing and turn endpointing.

The pure endpointing state machines (`Endpointer`, `BargeInDetector`) carry NO
torch dependency, so they are deterministic and unit-testable anywhere. The
torch model wrapper (`SileroVad`) imports torch and silero_vad lazily inside
`load()` and frames incoming PCM16 into exactly 512-sample windows, which is the
only window length the 16 kHz Silero model accepts (480/30 ms is rejected).
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import AsyncIterator, Optional

import numpy as np

from osvoice.audio import pcm16_to_float32
from osvoice.contracts import INPUT_SAMPLE_RATE

logger = logging.getLogger("osvoice.vad")

# Silero @16 kHz only accepts 512-sample (32 ms) windows; 480/30 ms is rejected.
SILERO_FRAME_SAMPLES = 512
_BYTES_PER_SAMPLE = 2  # PCM16 mono
_SILERO_FRAME_BYTES = SILERO_FRAME_SAMPLES * _BYTES_PER_SAMPLE


class VadEvent(str, Enum):
    """Endpointer transitions emitted at most once per frame."""

    SPEECH_START = "speech_start"
    TURN_END = "turn_end"


class Endpointer:
    """Hysteresis-based speech endpointing over per-frame speech probabilities.

    Pure (no torch). Enters SPEAKING after `speech_frames_to_start` consecutive
    frames above `start_threshold`, stays speaking while prob stays above
    `sustain_threshold`, and emits a turn end once accumulated consecutive
    silence reaches `silence_ms`.
    """

    def __init__(
        self,
        start_threshold: float = 0.6,
        sustain_threshold: float = 0.35,
        silence_ms: int = 500,
        frame_ms: int = 32,
        speech_frames_to_start: int = 2,
    ) -> None:
        if start_threshold < sustain_threshold:
            raise ValueError("start_threshold must be >= sustain_threshold")
        if frame_ms <= 0:
            raise ValueError("frame_ms must be positive")
        if speech_frames_to_start < 1:
            raise ValueError("speech_frames_to_start must be >= 1")
        self._start_threshold = start_threshold
        self._sustain_threshold = sustain_threshold
        self._silence_ms = silence_ms
        self._frame_ms = frame_ms
        self._speech_frames_to_start = speech_frames_to_start
        self._speaking = False
        self._consecutive_speech = 0
        self._silence_ms_acc = 0

    def reset(self) -> None:
        """Clear all state between turns."""
        self._speaking = False
        self._consecutive_speech = 0
        self._silence_ms_acc = 0

    def update(self, prob: float) -> Optional[VadEvent]:
        """Feed one frame's speech probability; return a transition event or None."""
        if not self._speaking:
            return self._update_idle(prob)
        return self._update_speaking(prob)

    def _update_idle(self, prob: float) -> Optional[VadEvent]:
        if prob > self._start_threshold:
            self._consecutive_speech += 1
            if self._consecutive_speech >= self._speech_frames_to_start:
                self._speaking = True
                self._silence_ms_acc = 0
                self._consecutive_speech = 0
                return VadEvent.SPEECH_START
        else:
            self._consecutive_speech = 0
        return None

    def _update_speaking(self, prob: float) -> Optional[VadEvent]:
        if prob > self._sustain_threshold:
            self._silence_ms_acc = 0
            return None
        self._silence_ms_acc += self._frame_ms
        if self._silence_ms_acc >= self._silence_ms:
            self.reset()
            return VadEvent.TURN_END
        return None


class BargeInDetector:
    """Debounced speech detector used during TTS playback.

    Requires `speech_frames` consecutive frames above `threshold` before
    signalling barge-in, which rejects brief TTS echo / transients. Pure
    (no torch).
    """

    def __init__(
        self,
        threshold: float = 0.6,
        speech_frames: int = 4,
    ) -> None:
        if speech_frames < 1:
            raise ValueError("speech_frames must be >= 1")
        self._threshold = threshold
        self._speech_frames = speech_frames
        self._consecutive_speech = 0

    def reset(self) -> None:
        """Clear the consecutive-speech counter."""
        self._consecutive_speech = 0

    def update(self, prob: float) -> bool:
        """Feed one frame's probability; return True once barge-in is confirmed."""
        if prob <= self._threshold:
            self._consecutive_speech = 0
            return False
        self._consecutive_speech += 1
        if self._consecutive_speech >= self._speech_frames:
            self._consecutive_speech = 0
            return True
        return False


class SileroVad:
    """Torch-backed Silero VAD that yields per-512-sample speech probabilities."""

    def __init__(self) -> None:
        self._model = None
        self._torch = None

    async def load(self) -> None:
        """Load the Silero model, pin threads to 1, and warm with a zero frame."""
        try:
            import torch
            from silero_vad import load_silero_vad
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "silero_vad and torch are required for SileroVad; install the "
                "voice extras to use the VAD backend"
            ) from exc
        try:
            torch.set_num_threads(1)
            model = load_silero_vad()
            warm = torch.zeros(SILERO_FRAME_SAMPLES, dtype=torch.float32)
            model(warm, INPUT_SAMPLE_RATE)
            model.reset_states()
        except Exception as exc:
            raise RuntimeError(f"Failed to load Silero VAD model: {exc}") from exc
        self._torch = torch
        self._model = model
        logger.info("Silero VAD loaded (frame=%d samples)", SILERO_FRAME_SAMPLES)

    async def aclose(self) -> None:
        """Release the model reference."""
        self._model = None
        self._torch = None

    def reset(self) -> None:
        """Reset the model's recurrent state between turns."""
        if self._model is not None:
            self._model.reset_states()

    async def probs(self, audio: AsyncIterator[bytes]) -> AsyncIterator[float]:
        """Frame PCM16 bytes into 512-sample windows; yield one probability each.

        Partial trailing bytes are buffered across chunks; remaining bytes shorter
        than one window are dropped on stream end (no zero-padding of real audio).
        """
        if self._model is None or self._torch is None:
            raise RuntimeError("SileroVad.load() must be called before probs()")
        buffer = bytearray()
        async for chunk in audio:
            buffer.extend(chunk)
            while len(buffer) >= _SILERO_FRAME_BYTES:
                window = bytes(buffer[:_SILERO_FRAME_BYTES])
                del buffer[:_SILERO_FRAME_BYTES]
                yield self._infer(window)

    def _infer(self, window: bytes) -> float:
        """Run one window through the model and return the speech probability."""
        samples = pcm16_to_float32(window)
        tensor = self._torch.from_numpy(np.ascontiguousarray(samples)).float()
        return self._model(tensor, INPUT_SAMPLE_RATE).item()
