"""Provider protocols and shared data types for osvoice.

Importing this module must stay cheap and dependency-light (no mlx / torch), so
the pure-logic layers and the test suite can import it without heavy backends.
Every backend adapter implements one of the slot protocols plus the `Provider`
lifecycle (load once, stream many, close).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable

# Canonical audio formats at the pipeline boundaries (PCM16 mono little-endian).
INPUT_SAMPLE_RATE = 16_000   # mic / STT input
OUTPUT_SAMPLE_RATE = 24_000  # default TTS output; read the model's rate when it exposes one


@dataclass(frozen=True)
class Transcript:
    """A single STT result. `is_final` marks an endpointed (turn-complete) utterance."""

    text: str
    is_final: bool


@runtime_checkable
class Provider(Protocol):
    """Common lifecycle shared by every backend adapter."""

    async def load(self) -> None:
        """Load weights and run one warmup inference. Called once during lifespan."""
        ...

    async def aclose(self) -> None:
        """Release any resources. Called on shutdown."""
        ...


class STTProvider(Provider, Protocol):
    def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        """Consume PCM16 @16 kHz mono frames; yield growing partials then a final Transcript."""
        ...


class LLMProvider(Provider, Protocol):
    def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yield assistant token deltas for an OpenAI-style chat `messages` list."""
        ...


class TTSProvider(Provider, Protocol):
    def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        """Consume clause strings; yield PCM16 mono speech frames (see OUTPUT_SAMPLE_RATE)."""
        ...
