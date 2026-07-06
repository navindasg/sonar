# overlay/

Native **Swift `LSUIElement` menu-bar app** — the visible face. Catches the **F13** hotkey (F5
remapped upstream, see `scripts/`), draws a **per-display dark edge-glow** via a non-activating,
click-through `NSPanel` (`.screenSaver` level, `.canJoinAllSpaces + .fullScreenAuxiliary`), and
connects to the voice pipeline over a **localhost WebSocket** to reflect state
(`idle/listening/thinking/speaking`, `level` from mic RMS / TTS amplitude).

**Status:** not built yet. Prototype the glow look/feel in `spike/` (Hammerspoon) first — spike **S1**.
Glow technique: inverted-palette `AppleIntelligenceGlowEffect` (angular gradient + blurred bloom).
