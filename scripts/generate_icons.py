#!/usr/bin/env python3
"""Generate the YouTube Agent app icon set — programmatically, no assets.

Round 6 (Tauri shell completion). The source art is DRAWN here with
Pillow at 4x supersample, saved as the 1024x1024 master, and every
shipped size is derived from that master with LANCZOS downsampling
only (never re-drawn, never upscaled).

Design (UI identity — dark + violet):
  * Rounded-rectangle tile filled #0B0E13 with a very subtle vertical
    gradient to a slightly lighter dark (#151A23) at the bottom.
  * A bold violet play triangle, centred (optically shifted right),
    filled with a #8B7CFF -> #6E5BFF vertical gradient.
  * A thin violet ring (#8B7CFF) around the triangle — a play-badge
    cue that still reads at large sizes and harmlessly merges into
    the violet mark at 16 px.

Violet provenance: sampled from the UI mockups
(~/workspace/youtube-windows-agent/ui-mockups/01_mission_control.png).
The dominant accent cluster in the mockup measures ~#6C59FB /
#7C68FC (buttons, progress bars), and the shipped frontend defines
the same accent in src/index.css as --color-accent: #6E5BFF with
--color-accent2: #8B7CFF. Those two CSS values are used here, so the
icon matches the running app exactly.

Requirements: any python3 with Pillow (developed against the backend
venv: backend/.venv/bin/python, Pillow 13). No other dependencies.

Usage (from the repo root):
    python3 scripts/generate_icons.py
    # or with the backend venv:
    backend/.venv/bin/python scripts/generate_icons.py

Outputs (src-tauri/icons/):
    source-1024.png   the drawn master (1024x1024)
    icon.png          512x512
    32x32.png         32x32
    128x128.png       128x128
    128x128@2x.png    256x256
    icon.ico          multi-size: 16/20/24/32/48/64/128/256
    icon.icns         Pillow ICNS: 16/32/64/128/256/512/1024

The script self-checks at the end: the 16x16 rendering must still
contain a clearly violet centre mark (the triangle), otherwise it
exits non-zero instead of shipping an illegible icon.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# ---------------------------------------------------------------- colours
TILE_TOP = (0x0B, 0x0E, 0x13)        # #0B0E13 — app background
TILE_BOTTOM = (0x15, 0x1A, 0x23)     # #151A23 — slightly lighter dark
VIOLET = (0x6E, 0x5B, 0xFF)          # #6E5BFF — sampled mockup accent / CSS --color-accent
VIOLET_LIGHT = (0x8B, 0x7C, 0xFF)    # #8B7CFF — CSS --color-accent2

MASTER = 1024                        # shipped master size
SS = 4                               # supersample factor while drawing
W = MASTER * SS                      # drawing canvas: 4096x4096

REPO_ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = REPO_ROOT / "src-tauri" / "icons"

ICO_SIZES = [(s, s) for s in (16, 20, 24, 32, 48, 64, 128, 256)]
ICNS_SIZES = [(s, s) for s in (16, 32, 64, 128, 256, 512, 1024)]
PNG_SIZES = {
    "icon.png": 512,
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
}


def vertical_gradient(size: int, top: tuple[int, int, int],
                      bottom: tuple[int, int, int]) -> Image.Image:
    """A size x size RGB image, `top` colour at row 0 -> `bottom` at the end.

    Built from a 1-pixel-wide strip so it needs nothing beyond Pillow.
    """
    strip = Image.new("RGB", (1, size))
    px = []
    for y in range(size):
        t = y / max(size - 1, 1)
        px.append(tuple(round(top[i] + (bottom[i] - top[i]) * t)
                        for i in range(3)))
    strip.putdata(px)
    return strip.resize((size, size))


def draw_master() -> Image.Image:
    """Draw the artwork at supersample size, return the 1024 master."""
    canvas = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # --- tile: dark vertical gradient inside a rounded rectangle -------
    tile = vertical_gradient(W, TILE_TOP, TILE_BOTTOM).convert("RGBA")
    tile_mask = Image.new("L", (W, W), 0)
    md = ImageDraw.Draw(tile_mask)
    radius = int(W * 0.215)  # ~22% corner radius, squircle-like
    md.rounded_rectangle([0, 0, W - 1, W - 1], radius=radius, fill=255)
    canvas.paste(tile, (0, 0), tile_mask)

    cx = cy = W // 2

    # --- thin violet ring around the play mark --------------------------
    ring = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    ring_r = int(W * 0.288)          # ring radius
    ring_w = max(int(W * 0.020), 2)  # thin stroke
    rd.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
               outline=VIOLET_LIGHT + (255,), width=ring_w)
    canvas.alpha_composite(ring)

    # --- bold play triangle (violet gradient, rounded corners) ----------
    tri_grad = vertical_gradient(W, VIOLET_LIGHT, VIOLET).convert("RGBA")
    tri_mask = Image.new("L", (W, W), 0)
    td = ImageDraw.Draw(tri_mask)
    half_h = int(W * 0.185)          # triangle half-height
    half_w = int(W * 0.160)          # triangle half-width
    shift = int(W * 0.020)           # optical centring: nudge right
    apex = (cx + shift + half_w, cy)
    top_v = (cx + shift - half_w, cy - half_h)
    bot_v = (cx + shift - half_w, cy + half_h)
    td.polygon([apex, top_v, bot_v], fill=255)
    # Soften the three corners with a small blur on the mask (rounded
    # vertices without the bumps that vertex-blob tricks leave behind).
    # Kept small on purpose: ~2px at master size rounds the corners
    # while the straight edges stay crisp next to the ring.
    tri_mask = tri_mask.filter(ImageFilter.GaussianBlur(int(W * 0.0022)))
    canvas.paste(tri_grad, (0, 0), tri_mask)

    # --- downsample the supersampled drawing to the 1024 master ---------
    return canvas.resize((MASTER, MASTER), Image.LANCZOS)


def check_legible_at_16(master: Image.Image) -> int:
    """Count saturated-violet core pixels in the centre of a 16x16 render.

    At 16x16 most of the triangle is edge-blend pixels, so the honest
    test is not total violet area but whether a clearly saturated
    violet core survives in the middle of the tile (rows/cols 4..11):
    blue channel >= 100 and clearly dominant over red. Measured on the
    shipped art: a solid ~4x4 core (values up to 141,122,255).
    """
    small = master.resize((16, 16), Image.LANCZOS).convert("RGBA")
    core = 0
    for y in range(4, 12):
        for x in range(4, 12):
            r, g, b, a = small.getpixel((x, y))
            if a > 128 and b >= 100 and b > r + 30:
                core += 1
    return core


def main() -> int:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    master = draw_master()

    master_path = ICONS_DIR / "source-1024.png"
    master.save(master_path)
    print(f"wrote {master_path.relative_to(REPO_ROOT)} "
          f"({master.size[0]}x{master.size[1]})")

    for name, size in PNG_SIZES.items():
        out = ICONS_DIR / name
        master.resize((size, size), Image.LANCZOS).save(out)
        print(f"wrote {out.relative_to(REPO_ROOT)} ({size}x{size})")

    ico_path = ICONS_DIR / "icon.ico"
    master.save(ico_path, format="ICO", sizes=ICO_SIZES)
    print(f"wrote {ico_path.relative_to(REPO_ROOT)} sizes={ICO_SIZES}")

    icns_path = ICONS_DIR / "icon.icns"
    master.save(icns_path, format="ICNS", sizes=ICNS_SIZES)
    print(f"wrote {icns_path.relative_to(REPO_ROOT)} sizes={ICNS_SIZES}")

    violet16 = check_legible_at_16(master)
    print(f"legibility check: {violet16} saturated-violet core pixels "
          f"in the centre of the 16x16 render")
    if violet16 < 8:
        print("ERROR: play triangle does not survive at 16x16 — "
              "icon would be illegible; refusing to ship it.",
              file=sys.stderr)
        return 1
    print("legibility check: PASS (triangle survives at 16x16)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
