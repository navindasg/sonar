# Vendored: osvoice

Source-only copy of **osvoice**, vendored into Sonar to be adapted (not used as-is).

- **Upstream:** `git@github.com:navindasg/osvoice.git`
- **Vendored at commit:** `b11f3ff`
- **Vendored on:** 2026-07-06
- **Excluded:** `.git`, `.venv`, caches, `*.wav` smoke outputs.

## Why it's here / what changes

osvoice provides the MLX voice loop (STT · silero-VAD · TTS/Kokoro · barge-in) and a
provider-agnostic LM registry. For Sonar, the **LM section gets rewired** to route voice
turns through the lightweight tool-aware harness instead of the plain
`openai:<base_url>#<model>` pass-through — so a spoken turn becomes "STT → harness (tools +
memory) → TTS". Precedent for this rewrite: `~/workspace/os-int`'s `pipeline_tools.py`.

Keep the network-LLM path (it bypasses the capacity-1 `MLX_LIMITER`, so the harness call
won't contend with STT/TTS on Metal). Runs on the M3 Max. Python 3.12 only.
