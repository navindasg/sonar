"""Slot backend tables and the spec resolver.

Each slot (STT / LLM / TTS) maps a short scheme name to the adapter *class* that
implements it. Importing the adapter classes here is cheap: every adapter keeps
its heavy, Apple-Silicon-only backend imports (mlx, mlx_audio, parakeet_mlx,
torch, ollama, httpx) behind lazy imports inside `load()`/`stream()`, so the
registry — and the CLI `list` command that reads it — works with none of those
backends installed.

A spec is ``scheme:rest`` (split on the FIRST colon only): the scheme selects a
table entry and `rest` is passed verbatim to the adapter constructor. Two
fallbacks keep ad-hoc specs working: a bare ``hf:<repo>`` and any unrecognized
scheme both route to the slot's ``mlx`` adapter (with the original spec for the
unrecognized case, so e.g. ``mlx-community/x`` reaches the model loader intact).
"""
from __future__ import annotations

from typing import Final

from osvoice.contracts import LLMProvider, STTProvider, TTSProvider
from osvoice.providers.llm_mlx import MLXLM
from osvoice.providers.llm_ollama import OllamaLLM
from osvoice.providers.llm_openai import OpenAICompatLLM
from osvoice.providers.parakeet import ParakeetMLX
from osvoice.providers.stt_mlxaudio import MLXAudioSTT
from osvoice.providers.tts_kokoro import KokoroTTS
from osvoice.providers.tts_mlxaudio import MLXAudioTTS

# Per-slot scheme -> adapter class. The "mlx" entry is also the resolver's
# fallback for `hf:` and for any scheme the slot does not recognize.
STT: Final[dict[str, type]] = {
    "parakeet": ParakeetMLX,
    "mlx": MLXAudioSTT,
    "whisper": MLXAudioSTT,
    "qwen3-asr": MLXAudioSTT,
}

LLM: Final[dict[str, type]] = {
    "ollama": OllamaLLM,
    "mlx": MLXLM,
    "openai": OpenAICompatLLM,
}

TTS: Final[dict[str, type]] = {
    "kokoro": KokoroTTS,
    "mlx": MLXAudioTTS,
}

_TABLES: Final[dict[str, dict[str, type]]] = {"stt": STT, "lm": LLM, "tts": TTS}

# Specs of this scheme carry only a model repo id; route them to the mlx adapter.
_HF_SCHEME: Final = "hf"


def resolve(slot: str, spec: str) -> STTProvider | LLMProvider | TTSProvider:
    """Instantiate the backend for ``slot`` (``"stt"``/``"lm"``/``"tts"``) from ``spec``.

    The scheme is the text before the FIRST colon; the remainder is handed to the
    adapter constructor unchanged. ``hf:<repo>`` and unknown schemes fall back to
    the slot's mlx adapter (the unknown case keeps the full spec, so a bare repo
    id like ``mlx-community/...`` still reaches the loader).
    """
    try:
        table = _TABLES[slot]
    except KeyError as exc:
        raise ValueError(
            f"unknown slot {slot!r}; expected one of {sorted(_TABLES)}"
        ) from exc

    scheme, sep, rest = spec.partition(":")
    if sep and scheme in table:
        return table[scheme](rest)
    if scheme == _HF_SCHEME:
        return table["mlx"](rest)
    return table["mlx"](spec)


def registered_backends() -> dict[str, list[str]]:
    """Return the available scheme names per slot, for the CLI ``list`` command."""
    return {slot: sorted(table) for slot, table in _TABLES.items()}
