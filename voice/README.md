# osvoice

Local, provider-agnostic voice-to-voice for Apple Silicon. Everything — speech
recognition, the language model, and speech synthesis — runs on-device; no audio
or text leaves the machine.

## Pipeline

```
mic PCM ─▶ VAD/endpoint ─▶ STT ─▶ LLM ─▶ clause aggregator ─▶ TTS ─▶ playback
  16 kHz    (silero-vad)  (partials) (deltas)  (clauses)      (24 kHz)
                                                                    │
                          ◀──────── barge-in (new speech) ─────────┘
```

Mic frames are endpointed by a VAD, streamed to STT for growing partials, fed
to the LLM as a chat turn, split into clauses as tokens arrive, and synthesized
clause-by-clause so audio starts playing before the full answer exists. Speaking
into the mic mid-response triggers barge-in: playback stops and the new turn
takes over. Target voice-to-voice latency is ~700–800 ms.

## Requirements

- Apple Silicon (M-series). MLX inference is Metal-only.
- macOS with microphone access granted to the browser/OS.
- Python 3.12 (`>=3.12,<3.13`).
- An [Ollama](https://ollama.com) daemon for the default LLM slot.

## Install

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# In a separate shell: keep the model resident and serve it.
OLLAMA_KEEP_ALIVE=-1 ollama serve
ollama pull gemma4:e4b-mlx
```

`OLLAMA_KEEP_ALIVE=-1` pins the model in memory so the first turn isn't paying a
cold load.

## Quickstart

```sh
# Start the server with the default slots.
osvoice serve --stt parakeet:mlx-community/parakeet-tdt-0.6b-v3 \
              --lm ollama:gemma4:e4b-mlx \
              --tts kokoro:af_heart

# Then open the web client in a browser and grant mic access:
open http://127.0.0.1:9753
```

With no flags, `osvoice serve` uses the defaults below, so a bare `osvoice serve`
(plus a running Ollama daemon) is enough to get going.

## CLI

| Command            | Description                                              |
| ------------------ | ------------------------------------------------------- |
| `osvoice serve`    | Resolve the three slots and run the server.             |
| `osvoice list`     | Print the registered backends available per slot.       |
| `osvoice doctor`   | Best-effort environment checks (imports, Ollama, mic).  |

`serve` flags: `--stt`, `--lm`, `--tts` (slot specs), `--host`, `--port`,
`--metrics` (log per-turn latency).

## Spec format

Each slot is configured by a `backend:model` spec. The **backend prefix is the
provider toggle** — it selects which adapter loads the model:

```
parakeet:mlx-community/parakeet-tdt-0.6b-v3   # named backend + model id
kokoro:af_heart
ollama:gemma4:e4b-mlx                          # split on the FIRST colon only
```

Routing rules:

- `backend:rest` — if `backend` is a registered scheme for that slot, the
  adapter receives `rest` verbatim.
- A **bare HF repo** (`mlx-community/...`), an explicit `hf:<repo>`, or any
  unrecognized scheme routes to the slot's **mlx-audio loader** — the catch-all
  that loads arbitrary MLX models for that slot.
- `openai:<base_url>#<model>` — an OpenAI-compatible streaming endpoint
  (LM Studio, vLLM, llama.cpp server, …); the `#model` fragment names the model,
  e.g. `openai:http://localhost:1234/v1#qwen3`.

Run `osvoice list` to see the registered scheme names for each slot.

## Default models

| Slot | Spec                                          | Backend           |
| ---- | --------------------------------------------- | ----------------- |
| lm   | `ollama:gemma4:e4b-mlx`                        | Ollama (async HTTP) |
| stt  | `parakeet:mlx-community/parakeet-tdt-0.6b-v3`  | parakeet-mlx      |
| tts  | `kokoro:af_heart`                              | mlx-audio (Kokoro) |

## Dev / testing

```sh
pytest -q
```

The test suite covers the **MLX-free core** — the resolver, clause aggregator,
VAD logic, and pipeline wiring. Adapter modules import their heavy,
Apple-Silicon-only backends (mlx, parakeet-mlx, mlx-audio, torch, silero-vad)
*lazily inside their methods*, so importing the registry and exercising the core
logic works on any machine without those packages or pulled models installed.
Provider adapters are excluded from the coverage bar for the same reason.

## Concurrency note

MLX releases the GIL during eval but is **not thread-safe for concurrent
evals** — two evals racing the Metal backend can crash the process. So every
blocking MLX call (STT, LLM, and TTS alike) is funneled through a single
process-wide capacity limiter, `runtime.MLX_LIMITER` (capacity 1), via
`runtime.offload()` / `runtime.stream_sync()`. At most one eval is ever in
flight. Offloading to a worker thread keeps the asyncio event loop responsive so
audio I/O never blocks; it does **not** buy parallel inference, by design.
Network backends (Ollama, OpenAI-compatible) are plain async HTTP and bypass the
limiter entirely.
