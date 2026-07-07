# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets>=13"]
# ///
"""Sonar S1 fake-state stub WebSocket server.

Stands in for the real voice pipeline so the Hammerspoon glow spike has
something to react to. On each client connection it cycles the assistant
states idle -> listening -> thinking -> speaking (~1.5s each) and streams
JSON envelopes {"state": <name>, "level": <0..1>}, where `level` gently
oscillates so the glow's intensity visibly breathes.

Usage:
    uv run stub_server.py                 # cycle all states
    uv run stub_server.py listening       # hold one fixed state for eyeballing
    uv run stub_server.py --help
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time

import websockets

# Default 8770 (8765 commonly collides with a Docker/ultraseek listener).
# Override with SONAR_GLOW_HOST / SONAR_GLOW_PORT; the glow client (init.lua)
# shares the same defaults so they line up without any coordination.
HOST = os.environ.get("SONAR_GLOW_HOST", "127.0.0.1")
PORT = int(os.environ.get("SONAR_GLOW_PORT", "8770"))

STATES = ("idle", "listening", "thinking", "speaking")
STATE_PERIOD = 1.5  # seconds each state is held while cycling
TICK = 0.12         # send cadence; state advances every STATE_PERIOD

# Resting intensity and oscillation amplitude per state. Idle is near-off;
# speaking is the brightest and liveliest.
_BASE = {"idle": 0.05, "listening": 0.45, "thinking": 0.60, "speaking": 0.85}
_AMP = {"idle": 0.02, "listening": 0.15, "thinking": 0.20, "speaking": 0.15}


def level_for(state: str, t: float) -> float:
    """Return a smoothly oscillating level in [0, 1] for `state` at time `t`."""
    base = _BASE.get(state, 0.0)
    amp = _AMP.get(state, 0.0)
    osc = math.sin(t * 2.0)  # ~0.32 Hz
    return max(0.0, min(1.0, base + amp * osc))


def make_handler(fixed_state: str | None):
    """Build a connection handler bound to an optional fixed state."""

    async def handler(websocket) -> None:
        remote = getattr(websocket, "remote_address", None)
        print(f"[stub] client connected: {remote}")
        t0 = time.monotonic()
        try:
            while True:
                t = time.monotonic() - t0
                if fixed_state is not None:
                    state = fixed_state
                else:
                    idx = int(t / STATE_PERIOD) % len(STATES)
                    state = STATES[idx]
                payload = {"state": state, "level": round(level_for(state, t), 3)}
                await websocket.send(json.dumps(payload))
                await asyncio.sleep(TICK)
        except websockets.ConnectionClosed:
            pass
        finally:
            print(f"[stub] client disconnected: {remote}")

    return handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sonar S1 fake-state stub WebSocket server.")
    parser.add_argument(
        "state",
        nargs="?",
        choices=STATES,
        default=None,
        help="Hold one fixed state instead of cycling (for eyeballing a single look).",
    )
    return parser.parse_args()


async def serve(fixed_state: str | None) -> None:
    handler = make_handler(fixed_state)
    async with websockets.serve(handler, HOST, PORT):
        mode = f"fixed '{fixed_state}'" if fixed_state else "cycling all states"
        print(f"[stub] serving ws://{HOST}:{PORT} ({mode}); Ctrl-C to stop")
        await asyncio.Future()  # run forever


def main() -> None:
    # Line-buffer stdout so [stub] logs appear promptly even when piped to a file.
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    try:
        asyncio.run(serve(args.state))
    except KeyboardInterrupt:
        print("\n[stub] shutting down")


if __name__ == "__main__":
    main()
