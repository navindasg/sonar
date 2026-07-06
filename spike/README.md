# spike/

Throwaway prototypes to lock look/feel and prove seams **before** committing real code. Keep the
spikes that stick; swap the ones that don't.

- **S1 — glow + hotkey (Hammerspoon):** `hidutil` F5→F13 remap + `hs.hotkey` on F13 + `hs.canvas`
  dark pulsing per-display rim + `hs.websocket` to a stub server emitting `listening/thinking/speaking`.
  Pass bar: press key → dark glow on all displays, click-through, driven by fake states.
  _(Hammerspoon isn't installed yet — `brew install hammerspoon`.)_

Other spikes (S2 voice loop, S3 harness seam, S4 voice↔harness, S5 email OAuth, S6 worker+state)
live next to the component they exercise or here, as convenient.
