"""osvoice command-line interface (Typer).

`serve` resolves each slot spec through the registry, assembles the providers
dict and runs the FastAPI app under uvicorn. `list` prints the registered
backends per slot. `doctor` runs best-effort environment checks and reports
them without ever crashing. User-facing output goes through `typer.echo`.

Heavy backend libraries are never imported here: registry import is cheap (the
adapters lazy-import their backends), and uvicorn/build_app only touch the app
factory, not the models, until `serve` actually runs.
"""
from __future__ import annotations

import logging

import typer

from osvoice import registry

logger = logging.getLogger("osvoice.cli")

app = typer.Typer(add_completion=False, help="Local voice-to-voice server for Apple Silicon.")

# Pipeline defaults: streaming Parakeet STT, Kokoro TTS, local Ollama LLM.
_DEFAULT_STT = "parakeet:mlx-community/parakeet-tdt-0.6b-v3"
_DEFAULT_TTS = "kokoro:af_heart"
_DEFAULT_LM = "ollama:gemma4:e4b-mlx"
# Uncommon default port (odd-digit ladder) to avoid clashing with the usual
# dev/ML servers (8080/8000/3000/11434/1234/7860/5173). Override with --port.
_DEFAULT_PORT = 9753

_OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
_FOUNDATION_MODULES = (
    "osvoice.contracts",
    "osvoice.runtime",
    "osvoice.audio",
    "osvoice.aggregator",
    "osvoice.vad",
    "osvoice.metrics",
)


@app.command()
def serve(
    stt: str = typer.Option(_DEFAULT_STT, help="STT spec, e.g. 'parakeet:<repo>'."),
    tts: str = typer.Option(_DEFAULT_TTS, help="TTS spec, e.g. 'kokoro:af_heart'."),
    lm: str = typer.Option(_DEFAULT_LM, help="LLM spec, e.g. 'ollama:<model>'."),
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(_DEFAULT_PORT, help="Bind port."),
    metrics: bool = typer.Option(False, help="Log per-turn latency metrics."),
) -> None:
    """Resolve the providers and run the voice server."""
    import uvicorn

    from osvoice.app import build_app

    providers = _resolve_providers(stt, lm, tts)
    if metrics:
        logging.getLogger("osvoice").setLevel(logging.INFO)
    typer.echo(f"osvoice serving on http://{host}:{port}  (stt={stt} lm={lm} tts={tts})")
    uvicorn.run(build_app(providers), host=host, port=port)


def _resolve_providers(stt: str, lm: str, tts: str) -> dict[str, object]:
    """Resolve each slot spec into a provider instance via the registry."""
    try:
        return {
            "stt": registry.resolve("stt", stt),
            "lm": registry.resolve("lm", lm),
            "tts": registry.resolve("tts", tts),
        }
    except Exception as exc:  # noqa: BLE001 - turn config errors into clear CLI output
        typer.echo(f"error: could not resolve providers: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command(name="list")
def list_backends() -> None:
    """List the registered backends available for each slot."""
    for slot, schemes in registry.registered_backends().items():
        typer.echo(f"{slot}: {', '.join(schemes)}")


@app.command()
def doctor() -> None:
    """Run best-effort environment checks and print a readable report."""
    typer.echo("osvoice doctor")
    typer.echo("-" * 40)
    _check_foundation()
    _check_ollama()
    _note_mic_permissions()


def _check_foundation() -> None:
    """Verify the dependency-light foundation modules import cleanly."""
    import importlib

    for name in _FOUNDATION_MODULES:
        try:
            importlib.import_module(name)
            typer.echo(f"[ok]   import {name}")
        except Exception as exc:  # noqa: BLE001 - report, never crash
            typer.echo(f"[fail] import {name}: {exc}")


def _check_ollama() -> None:
    """Probe the local Ollama daemon; report reachability without failing."""
    try:
        import httpx

        resp = httpx.get(_OLLAMA_TAGS_URL, timeout=2.0)
        resp.raise_for_status()
        models = [m.get("name", "?") for m in resp.json().get("models", [])]
        typer.echo(f"[ok]   Ollama reachable ({len(models)} model(s) available)")
    except Exception as exc:  # noqa: BLE001 - daemon may simply be down
        typer.echo(f"[warn] Ollama not reachable at {_OLLAMA_TAGS_URL}: {exc}")


def _note_mic_permissions() -> None:
    """Remind the operator that the browser/OS must grant microphone access."""
    typer.echo(
        "[note] microphone access is granted by the browser/OS; if capture is "
        "silent, check System Settings > Privacy & Security > Microphone."
    )


def main() -> None:
    """Console-script entry point (`osvoice = osvoice.cli:main`)."""
    app()


if __name__ == "__main__":
    main()
