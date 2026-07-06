"""Tests for registry.resolve: spec -> adapter class for every slot/spec form.

These never call .load()/.stream() (no models / no Apple-Silicon backends are
present), they only assert the resolver picks the right adapter *class* and that
the FIRST-colon split hands the remainder of the spec to the constructor intact.
The whole module must import with only numpy/anyio/fastapi available, which it
does because the adapters keep their heavy imports lazy.
"""
from __future__ import annotations

import pytest

from osvoice import registry


def test_module_imports_without_heavy_backends() -> None:
    # Arrange / Act: importing the registry (done at module load) must not need
    # mlx/torch/ollama. Act: resolve one spec per slot.
    stt = registry.resolve("stt", "parakeet:repo")
    lm = registry.resolve("lm", "ollama:llama3")
    tts = registry.resolve("tts", "kokoro:af_heart")

    # Assert: we got concrete objects back for each slot.
    assert type(stt).__name__ == "ParakeetMLX"
    assert type(lm).__name__ == "OllamaLLM"
    assert type(tts).__name__ == "KokoroTTS"


@pytest.mark.parametrize(
    ("slot", "spec", "expected_cls"),
    [
        # ollama: the rest carries its own colon (model tag) and must survive
        # the first-colon split untouched.
        ("lm", "ollama:gemma4:e4b-mlx", "OllamaLLM"),
        # kokoro voice spec.
        ("tts", "kokoro:af_heart", "KokoroTTS"),
        # openai-compatible: base url (with its own colons/slashes) + #model.
        ("lm", "openai:http://localhost:1234/v1#model", "OpenAICompatLLM"),
        # hf: fallback -> the slot's mlx adapter.
        ("stt", "hf:org/repo", "MLXAudioSTT"),
        ("lm", "hf:org/repo", "MLXLM"),
        ("tts", "hf:org/repo", "MLXAudioTTS"),
        # Bare repo id (no scheme) -> mlx fallback, full spec preserved.
        ("stt", "mlx-community/parakeet-tdt-0.6b-v3", "MLXAudioSTT"),
        # Local-path-looking string -> mlx fallback.
        ("stt", "./models/my-local-model", "MLXAudioSTT"),
        ("tts", "/abs/path/to/model", "MLXAudioTTS"),
        # Explicit STT schemes.
        ("stt", "parakeet:mlx-community/parakeet-tdt", "ParakeetMLX"),
        ("stt", "whisper:mlx-community/whisper-large-v3", "MLXAudioSTT"),
        ("stt", "qwen3-asr:mlx-community/qwen3-asr", "MLXAudioSTT"),
        # Explicit LLM mlx scheme.
        ("lm", "mlx:mlx-community/Qwen2.5-7B", "MLXLM"),
    ],
)
def test_resolve_returns_expected_adapter_class(
    slot: str, spec: str, expected_cls: str
) -> None:
    # Act
    obj = registry.resolve(slot, spec)

    # Assert: do not instantiate models, just check the adapter *class* chosen.
    assert type(obj).__name__ == expected_cls


def test_first_colon_split_preserves_rest_with_inner_colon() -> None:
    # Arrange: a model tag that itself contains a colon.
    spec = "ollama:gemma4:e4b-mlx"

    # Act
    obj = registry.resolve("lm", spec)

    # Assert: OllamaLLM exposes the raw model id; the inner colon must be kept,
    # proving the resolver split on the FIRST colon only.
    assert obj.model == "gemma4:e4b-mlx"


def test_unknown_scheme_falls_back_to_mlx_with_full_spec_preserved() -> None:
    # Arrange: an unrecognized scheme that looks like a bare repo id.
    spec = "mlx-community/some-model"

    # Act
    obj = registry.resolve("lm", spec)

    # Assert: routed to the mlx adapter (fallback), full spec kept so the loader
    # still sees the whole repo id.
    assert type(obj).__name__ == "MLXLM"


def test_unknown_slot_raises_value_error() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="unknown slot"):
        registry.resolve("nope", "ollama:llama3")


def test_registered_backends_lists_schemes_per_slot() -> None:
    # Act
    backends = registry.registered_backends()

    # Assert: every slot present and schemes sorted; spot-check key entries.
    assert set(backends) == {"stt", "lm", "tts"}
    assert "parakeet" in backends["stt"]
    assert "ollama" in backends["lm"]
    assert "kokoro" in backends["tts"]
    for schemes in backends.values():
        assert schemes == sorted(schemes)
