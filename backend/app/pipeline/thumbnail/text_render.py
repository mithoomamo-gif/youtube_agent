"""Pillow text rendering helpers: fonts, wrapping, auto-fit, Arabic shaping.

HARD PRODUCT RULE: headline text on thumbnails is drawn here, with Pillow,
never by an AI image generator (see roadmap section 21). This module is the
only place thumbnail text is rasterised.

Urdu / Arabic-script support: if ``arabic_reshaper`` and ``python-bidi`` are
installed, Arabic-script headlines are reshaped and bidi-reordered before
drawing (Pillow draws left-to-right). If they are not installed we fall back
gracefully: the raw string is drawn as-is and ``shaped`` is reported False.
Latin text never needs shaping and always works.
"""

from __future__ import annotations

import glob
import os
from typing import Optional

from PIL import ImageDraw, ImageFont

# Ranges covering Arabic, Arabic Supplement/Extended and presentation forms
# (Urdu lives mostly in U+0600-U+06FF).
_ARABIC_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)

#: System bold fonts tried when a profile names no font. DejaVu Sans Bold and
#: Liberation Sans Bold are present on virtually every Linux/CI image; on
#: Windows the app ships its own fonts and sets the profile paths explicitly.
_FALLBACK_FONT_GLOBS = (
    "/usr/share/fonts/**/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/**/LiberationSans-Bold.ttf",
    "/usr/share/fonts/**/NotoSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


def contains_arabic_script(text: str) -> bool:
    return any(
        any(lo <= ord(ch) <= hi for lo, hi in _ARABIC_RANGES) for ch in text
    )


def shaping_available() -> bool:
    try:
        import arabic_reshaper  # noqa: F401
        import bidi.algorithm  # noqa: F401

        return True
    except Exception:
        return False


def shape_text(text: str) -> tuple[str, bool]:
    """Return (display_text, shaped).

    ``shaped`` is True only when Arabic-script text was actually reshaped +
    bidi-reordered. Latin text and the no-library fallback return the input
    unchanged with ``shaped=False``.
    """
    if not contains_arabic_script(text):
        return text, False
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text)), True
    except Exception:
        return text, False


def find_fallback_font() -> Optional[str]:
    for pattern in _FALLBACK_FONT_GLOBS:
        matches = sorted(glob.glob(pattern, recursive=True))
        if matches:
            return matches[0]
    return None


def load_font(font_path: Optional[str], size: int) -> ImageFont.FreeTypeFont:
    """Load a TrueType font at ``size`` px, falling back gracefully.

    Order: explicit path -> system bold fallback -> Pillow's built-in font.
    """
    candidates = [font_path, find_fallback_font()]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                continue
    # Last resort: Pillow's bitmap/default font (pixel size not honoured on
    # old Pillow; on Pillow >= 10.1 load_default accepts a size).
    try:
        return ImageFont.load_default(size=size)  # type: ignore[arg-type]
    except TypeError:
        return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font, stroke_width: int) -> float:
    if not text:
        return 0.0
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    return box[2] - box[0]


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: float,
    stroke_width: int = 0,
) -> list[str]:
    """Greedy word wrap. Words longer than max_width are kept whole on their
    own line (the auto-fit loop will shrink the font for them)."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if _text_width(draw, trial, font, stroke_width) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Optional[str],
    box_width: float,
    box_height: float,
    start_size: int,
    min_size: int = 26,
    stroke_ratio: float = 1 / 13,
    line_spacing: float = 1.08,
) -> tuple[ImageFont.FreeTypeFont, list[str], int, bool]:
    """Auto-shrink: largest font (<= start_size) whose wrapped text fits box.

    Returns (font, lines, font_size, fits). ``fits`` is False when even
    ``min_size`` overflows (caller should warn); lines are still returned.
    """
    size = start_size
    while True:
        font = load_font(font_path, size)
        stroke = max(2, round(size * stroke_ratio))
        lines = wrap_text(draw, text, font, box_width, stroke)
        line_h = size * line_spacing
        total_h = line_h * len(lines)
        widest = max(
            (_text_width(draw, ln, font, stroke) for ln in lines), default=0.0
        )
        if widest <= box_width and total_h <= box_height:
            return font, lines, size, True
        if size <= min_size:
            return font, lines, size, False
        size = max(min_size, size - 4)
