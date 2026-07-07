# S1 — glow spike (Hammerspoon + fake-state stub)

Throwaway prototype that locks the **signature look**: a dark, pulsing, per-display
screen-**edge** glow that reacts to fake assistant states (`idle → listening → thinking →
speaking`) — with **no** real voice stack. This nails the aesthetic before the native Swift
`NSPanel` overlay is written.

- `init.lua` — Hammerspoon config: per-display `hs.canvas` edge glow, click-through, floats
  over full-screen apps, breathing pulse, `hs.websocket` **client** to the stub server.
- `stub_server.py` — standalone `uv` script (PEP 723) that plays a fake voice pipeline over a
  localhost WebSocket.

> **Visual acceptance is yours.** A headless agent can confirm the wiring and message flow but
> cannot judge the look — you have to eyeball it on real displays.

## Prerequisites

- **Hammerspoon** (not required to build this, only to see it):
  ```sh
  brew install hammerspoon
  ```
  Launch Hammerspoon once and grant it the permission it asks for. (No Accessibility permission
  is needed for the fallback chord or for F13 once remapped.)
- **uv** (already installed) for the stub server. No manual `pip install` — the dependency
  (`websockets`) is declared inline in the script and fetched on first run.

## 1. Point Hammerspoon at this config

Hammerspoon loads `~/.hammerspoon/init.lua`. Symlink this spike's file (recommended so edits are
picked up in place):

```sh
mkdir -p ~/.hammerspoon
# back up any existing config first
[ -e ~/.hammerspoon/init.lua ] && mv ~/.hammerspoon/init.lua ~/.hammerspoon/init.lua.bak
ln -sf "$(pwd)/init.lua" ~/.hammerspoon/init.lua
```

Then in the Hammerspoon menu-bar icon choose **Reload Config** (or run
`hs.reload()` in the Hammerspoon console). You should see a brief on-screen alert:
`Sonar glow loaded — F13 or ⌘⌥⌃G to toggle`.

To restore your old config later: `mv ~/.hammerspoon/init.lua.bak ~/.hammerspoon/init.lua` and
reload.

## 2. Start the stub server

In this directory:

```sh
uv run stub_server.py
```

It binds `ws://127.0.0.1:8770` (override with `SONAR_GLOW_PORT`) and, per connected client, cycles
`idle → listening → thinking → speaking` (~1.5s each), streaming
`{"state": <name>, "level": <0..1>}`. The glow reconnects automatically, so you can start the
server before or after Hammerspoon.

Hold one state to study a single look:

```sh
uv run stub_server.py speaking     # or idle | listening | thinking
uv run stub_server.py --help
```

## 3. Toggle the glow and watch it cycle

- Press **F13** — the intended production key (F5 remapped upstream via `scripts/`; see
  `docs/DECISIONS.md`).
- Or press the fallback chord **⌘⌥⌃G** (`cmd+alt+ctrl+G`) — works immediately, no remap needed,
  so you can test before the `hidutil` LaunchAgent is installed.

With the stub server running you should see, on **every** display:

- `idle` — glow essentially off / a barely-there charcoal rim.
- `listening` — a cool dim blue breathing rim.
- `thinking` — an amber pulse, a little faster.
- `speaking` — a brighter warm ember pulse.

The rim is click-through (you can click straight through it) and floats above other windows,
including full-screen apps. Unplug/replug a display or change arrangement — it re-lays-out on its
own.

Press the toggle again to hide it.

## What "pass" looks like

Press key → dark edge glow on all displays → visibly click-through → colour/intensity driven by
the fake states from the stub server. Final aesthetic sign-off is a human judgement call.

## Notes / knobs

- Palette, layer count, frame rate, and reconnect delay are constants at the top of `init.lua`.
- Canvas level is `hs.canvas.windowLevels.screenSaver` so the rim reaches the true screen edge
  (above the menu bar / around the notch). Behaviour is
  `canJoinAllSpaces + stationary + fullScreenAuxiliary`.
- This is a **spike**. The real overlay is native Swift (`overlay/`); the message envelope
  (`{"state","level"}`) and state names are the contract that carries over.
