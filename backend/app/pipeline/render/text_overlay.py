"""On-screen text as pre-rendered PNG overlays (Pillow).

Rule (roadmap Part 6.7, hard): on-screen text is rendered to a transparent
PNG with Pillow and composited with ffmpeg's ``overlay`` filter.  The
ffmpeg ``drawtext`` filter is NEVER used for Urdu/Arabic (or any shaped /
complex-script) text: its shaping and ligatures depend on the build's font
stack and are unreliable, while a shaped PNG is deterministic and
pixel-identical on every machine — and cacheable as an asset.

``drawtext`` is acceptable ONLY for trivial Latin/ASCII dynamic counters
(e.g. a plain ``%{n}`` frame counter in a debug draft).  This module
deliberately provides no drawtext helper for real on-screen text; the one
sanctioned use is documented here so nobody re-invents it for titles.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

#: Candidate font locations, in order, when no explicit font is given.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def find_default_font() -> Path:
    """Return the first available fallback font on this machine."""
    for cand in _FONT_CANDIDATES:
        if Path(cand).exists():
            return Path(cand)
    raise FileNotFoundError(
        "No fallback font found; pass font_path explicitly "
        f"(looked for: {_FONT_CANDIDATES})"
    )


def render_text_png(
    text: str,
    output_path: Path,
    font_path: Path | str | None = None,
    font_size: int = 64,
    text_color: tuple[int, int, int, int] = (255, 255, 255, 255),
    stroke_width: int = 3,
    stroke_color: tuple[int, int, int, int] = (0, 0, 0, 255),
    padding: int = 16,
    shadow_offset: tuple[int, int] = (3, 3),
    shadow_color: tuple[int, int, int, int] = (0, 0, 0, 160),
) -> Path:
    """Render ``text`` to a transparent RGBA PNG and return its path.

    The PNG is tightly cropped to the text (plus padding and room for the
    stroke/shadow), so overlay positioning in the filter graph refers to
    the visible glyphs, not a full-frame canvas.

    For Urdu/Arabic text, pass a suitable font (e.g. Noto Nastaliq Urdu);
    Pillow's libraqm-backed layout (when available) shapes the text before
    rasterising — exactly what drawtext cannot be trusted to do.
    """
    if not text:
        raise ValueError("text must be non-empty")
    font_file = Path(font_path) if font_path else find_default_font()
    font = ImageFont.truetype(str(font_file), font_size)

    # Measure on a scratch surface, then draw once at the final size.
    scratch = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scratch)
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    width = text_w + 2 * padding + abs(shadow_offset[0])
    height = text_h + 2 * padding + abs(shadow_offset[1])

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    origin_x = padding - bbox[0]
    origin_y = padding - bbox[1]
    if shadow_offset != (0, 0):
        draw.text((origin_x + shadow_offset[0], origin_y + shadow_offset[1]),
                  text, font=font, fill=shadow_color,
                  stroke_width=stroke_width, stroke_fill=shadow_color)
    draw.text((origin_x, origin_y), text, font=font, fill=text_color,
              stroke_width=stroke_width, stroke_fill=stroke_color)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, format="PNG")
    return output_path
