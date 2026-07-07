# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy>=1.26", "pillow>=10"]
# ///
"""Generate the Sonar 'cave' vignette texture — a dark, grainy shadow that hugs
the screen edges and fades to a clear centre.

The overlay is a single static RGBA PNG (no color, no cycling): near-black at the
edges/corners, transparent in the middle, with organic low-frequency mottle plus
fine grain so it reads like a textured cave wall rather than a smooth gradient.
Hammerspoon (init.lua) draws it full-screen and only modulates its overall
opacity by state — it never changes hue.

    uv run gen_vignette.py --width 3456 --height 2234 --out vignette.png
"""

from __future__ import annotations

import argparse

import numpy as np
from PIL import Image


def smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def value_noise(h: int, w: int, scale: int, rng: np.random.Generator) -> np.ndarray:
    """Smooth organic mottle: low-res random field bicubically upsampled."""
    lh, lw = max(2, h // scale), max(2, w // scale)
    small = (rng.random((lh, lw)) * 255).astype(np.uint8)
    up = Image.fromarray(small).resize((w, h), Image.BICUBIC)
    return np.asarray(up, dtype=np.float32) / 255.0


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the Sonar cave vignette PNG.")
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-alpha", type=float, default=1.0, help="darkest edge opacity")
    ap.add_argument("--band", type=float, default=0.082, help="rim thickness as fraction of half-screen")
    ap.add_argument("--gamma", type=float, default=1.3, help="edge falloff shaping (>1 = crisper rim)")
    ap.add_argument("--spike", type=float, default=0.7, help="spiky inner-edge depth (0=smooth, 1=very spiky)")
    ap.add_argument("--spike-scale", type=int, default=10, help="spike size (bigger = larger, coarser spikes)")
    ap.add_argument("--blue-frac", type=float, default=0.16, help="outer fraction of the rim that's sonar-blue")
    ap.add_argument("--grain", type=float, default=0.18, help="fine film-grain depth")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    w, h = a.width, a.height
    rng = np.random.default_rng(a.seed)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = (xx / (w - 1)) * 2.0 - 1.0
    ny = (yy / (h - 1)) * 2.0 - 1.0

    # Distance to the nearest screen edge (0 at edge, growing inward).
    d = np.minimum(1.0 - np.abs(nx), 1.0 - np.abs(ny))

    # Spiky rim: vary band thickness with high-frequency noise so the inner
    # contour is jagged ("almost spikes") instead of a smooth line.
    spikefield = value_noise(h, w, scale=a.spike_scale, rng=rng) ** 1.6
    local_band = np.maximum(a.band * (1.0 - a.spike * (1.0 - spikefield)), a.band * 0.10)
    vig = np.power(np.clip(1.0 - smoothstep(0.0, local_band, d), 0.0, 1.0), a.gamma)

    # Fine grain for a gritty, noisy rim.
    grain = rng.random((h, w)).astype(np.float32)
    alpha = np.clip(vig * (1.0 + a.grain * (grain - 0.5) * 2.0) * a.max_alpha, 0.0, 1.0)

    # Colour: electric "sonar" blue at the very outer edge, fading to near-black
    # just inside it — the blue sits UNDER / OUTSIDE the black band.
    blue = np.array([0.26, 0.62, 1.00], dtype=np.float32)
    black = np.array([0.005, 0.005, 0.008], dtype=np.float32)
    t = smoothstep(0.0, a.band * a.blue_frac, d)[..., None]  # 0 edge(blue) -> 1 inner(black)
    rgb = blue[None, None, :] * (1.0 - t) + black[None, None, :] * t

    out = (np.dstack([rgb, alpha]) * 255.0).astype(np.uint8)
    Image.fromarray(out, "RGBA").save(a.out)
    print(
        f"wrote {a.out} ({w}x{h})  alpha mean={alpha.mean():.3f} "
        f"max={alpha.max():.3f}  band={a.band}"
    )


if __name__ == "__main__":
    main()
