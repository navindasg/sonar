"""Lightweight PCM conversion helpers (numpy only — no mlx).

Pipeline audio is PCM16 mono little-endian throughout. STT consumes 16 kHz, TTS
emits 24 kHz. These helpers convert between raw bytes and float32 samples in
[-1, 1]; adapters wrap the floats into an `mx.array` themselves, keeping mlx out
of this module so it stays cheap to import and easy to unit-test.
"""
from __future__ import annotations

import numpy as np

_PCM16_DTYPE = np.dtype("<i2")  # signed 16-bit little-endian


def pcm16_to_float32(data: bytes) -> np.ndarray:
    """Decode little-endian PCM16 bytes to a float32 array in [-1, 1]."""
    return np.frombuffer(data, dtype=_PCM16_DTYPE).astype(np.float32) / 32768.0


def float32_to_pcm16(samples: np.ndarray) -> bytes:
    """Encode float samples in [-1, 1] to little-endian PCM16 bytes (clipped)."""
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).astype(_PCM16_DTYPE).tobytes()
