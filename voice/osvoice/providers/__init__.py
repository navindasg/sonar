"""Backend adapter package for osvoice's STT / LLM / TTS slots.

Each submodule implements one of the slot protocols from `osvoice.contracts`
(`STTProvider`, `LLMProvider`, `TTSProvider`) plus the shared `Provider`
lifecycle. Adapters keep their heavy, Apple-Silicon-only dependencies
(mlx, mlx_lm, mlx_audio, parakeet_mlx, torch, silero_vad, ollama, httpx) behind
lazy imports inside `load()`/`stream()`, so importing this package — and the
resolver/registry that walk it — stays cheap and works without those backends
installed.

Nothing is re-exported here on purpose: importing `osvoice.providers` must not
pull any adapter module (and therefore any heavy backend) at import time. Import
the specific adapter submodule, or resolve it by name through the registry.
"""
from __future__ import annotations
