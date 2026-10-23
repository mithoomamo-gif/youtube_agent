"""Thumbnail builder (worker 5).

Composes a 1280x720 thumbnail with Pillow only:

    text-free background plate (cover-fitted/cropped to 16:9)
      -> palette gradient + vignette
      -> optional foreground / subject cutout in the layout's subject zone
      -> optional palette band (bottom-band layout)
      -> headline text, Pillow-rendered, auto-shrunk into the safe area,
         with a high-contrast stroke + shadow (Arabic-script shaping when
         arabic_reshaper + python-bidi are installed; graceful fallback else)
      -> accent bar in the profile's accent colour
      -> optional logo / watermark
      -> mobile-legibility check at 10% size

The headline is NEVER taken from an AI image generator -- backgrounds must
be text-free plates; all words are drawn here (hard product rule).
"""

from __future__ import annotations

import os
import re
from typing import Optional, Union

from PIL import Image, ImageDraw, ImageFilter

from .legibility import check_legibility
from .models import (
    MAX_HEADLINE_WORDS,
    SAFE_AREA,
    THUMBNAIL_HEIGHT,
    THUMBNAIL_WIDTH,
    ChannelStyleProfile,
    LayoutTemplate,
    ThumbnailResult,
    get_layout,
    hex_to_rgb,
)
from .text_render import contains_arabic_script, fit_text, load_font, shape_text

ImageLike = Union[str, os.PathLike, Image.Image]

_NUMBER_TOKEN = re.compile(r"^[^\d]?(\d[\d,\.]*)([%xX\+]?)$")


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------
def _open_image(source: ImageLike) -> Image.Image:
    if isinstance(source, Image.Image):
        return source.copy()
    return Image.open(source)


def fit_cover(image: Image.Image, size: tuple[int, int] = (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)) -> Image.Image:
    """Resize + centre-crop ``image`` so it exactly covers ``size`` (16:9)."""
    tw, th = size
    iw, ih = image.size
    scale = max(tw / iw, th / ih)
    resized = image.resize((round(iw * scale), round(ih * scale)), Image.LANCZOS)
    x = (resized.width - tw) // 2
    y = (resized.height - th) // 2
    return resized.crop((x, y, x + tw, y + th))


def _apply_gradient(canvas: Image.Image, layout: LayoutTemplate, dark: tuple[int, int, int]) -> None:
    """Directional dark gradient behind the text zone (readability)."""
    w, h = canvas.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if layout.band:
        grad = Image.linear_gradient("L").resize((1, h))  # 0 top -> 255 bottom
        alpha = grad.point(lambda v: int(v * 0.72))
        solid = Image.new("RGBA", (w, h), dark + (255,))
        solid.putalpha(alpha.resize((w, h)))
        overlay = Image.alpha_composite(overlay, solid)
    else:
        # Horizontal gradient: darkest on the left (behind the text).
        row = bytes(int((1 - x / max(1, w - 1)) * 0.78 * 255) for x in range(w))
        alpha = Image.frombytes("L", (w, 1), row).resize((w, h))
        solid = Image.new("RGBA", (w, h), dark + (255,))
        solid.putalpha(alpha)
        overlay = Image.alpha_composite(overlay, solid)
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB"), (0, 0))


def _apply_vignette(canvas: Image.Image) -> None:
    """Soft dark vignette (edges darkened, centre untouched)."""
    w, h = canvas.size
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((-int(w * 0.25), -int(h * 0.25), int(w * 1.25), int(h * 1.25)), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(120))
    black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    black.putalpha(mask.point(lambda v: int((255 - v) * 0.45)))
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), black).convert("RGB"), (0, 0))


def _intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
class ThumbnailBuilder:
    def __init__(self, profile: ChannelStyleProfile):
        self.profile = profile

    # -- public API ---------------------------------------------------------
    def build(
        self,
        background: ImageLike,
        headline: str,
        output_path: Union[str, os.PathLike],
        layout_id: Optional[str] = None,
        foreground: Optional[ImageLike] = None,
        variant_id: Optional[str] = None,
    ) -> ThumbnailResult:
        profile = self.profile
        layout = get_layout(layout_id or profile.default_layout_id)
        warnings: list[str] = []

        word_count = len(headline.split()) if headline else 0
        if word_count > MAX_HEADLINE_WORDS:
            warnings.append(
                f"HEADLINE_TOO_LONG: {word_count} words exceeds recommended "
                f"maximum of {MAX_HEADLINE_WORDS}"
            )

        canvas = fit_cover(_open_image(background).convert("RGB"))

        darkest = min(profile.palette_rgb(), key=lambda c: sum(c))
        if profile.use_gradient:
            _apply_gradient(canvas, layout, darkest)
        if profile.use_vignette:
            _apply_vignette(canvas)

        if foreground is not None:
            self._place_foreground(canvas, foreground, layout)

        draw = ImageDraw.Draw(canvas)

        if layout.band:
            band_colour = profile.palette_rgb()[0]
            draw.rectangle((0, 492, THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), fill=band_colour)

        # --- headline text (Pillow only) ------------------------------------
        arabic = contains_arabic_script(headline)
        display_text, shaped = shape_text(headline) if headline else ("", False)
        font_path = profile.font_path_for(arabic)
        text_box = _intersect(layout.text_box, SAFE_AREA)

        mask = Image.new("L", canvas.size, 0)
        font_size = 0
        text_bbox: Optional[tuple[int, int, int, int]] = None
        if headline.strip():
            font_size, text_bbox = self._draw_headline(
                canvas, mask, display_text, layout, text_box, font_path, warnings
            )
        else:
            warnings.append("HEADLINE_EMPTY: no headline text rendered")

        if profile.logo_path:
            self._place_logo(canvas, warnings)

        # --- save -------------------------------------------------------------
        output_path = str(output_path)
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        ext = os.path.splitext(output_path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            canvas.save(output_path, "JPEG", quality=92)
            image_format = "JPEG"
        else:
            canvas.save(output_path, "PNG")
            image_format = "PNG"

        # --- legibility -------------------------------------------------------
        if text_bbox is not None:
            legibility = check_legibility(canvas, text_bbox=text_bbox, text_mask=mask)
            if not legibility.passed:
                warnings.append("LEGIBILITY_FAIL: headline may be unreadable at mobile size")
        else:
            from .models import LegibilityResult

            legibility = LegibilityResult(
                passed=False, score=0.0, reasons=["no headline text"]
            )

        return ThumbnailResult(
            path=output_path,
            width=THUMBNAIL_WIDTH,
            height=THUMBNAIL_HEIGHT,
            image_format=image_format,
            headline=headline,
            layout_id=layout.id,
            style_profile=profile.name,
            word_count=word_count,
            font_size=font_size,
            text_bbox=text_bbox,
            shaped=shaped,
            warnings=warnings,
            legibility=legibility,
            variant_id=variant_id,
            metadata={
                "safe_area": SAFE_AREA,
                "palette": list(profile.palette),
                "accent_color": profile.accent_color or profile.palette[-1],
                "font_path": font_path,
                "arabic_script": arabic,
                "foreground": foreground is not None,
                "text_renderer": "pillow",  # never AI-generated text
            },
        )

    # -- internals ------------------------------------------------------------
    def _place_foreground(self, canvas: Image.Image, foreground: ImageLike, layout: LayoutTemplate) -> None:
        fg = _open_image(foreground)
        box = layout.subject_box
        bw, bh = box[2] - box[0], box[3] - box[1]
        if fg.mode in ("RGBA", "LA") or (fg.mode == "P" and "transparency" in fg.info):
            fg = fg.convert("RGBA")
            fg.thumbnail((bw, bh), Image.LANCZOS)
            x = box[0] + (bw - fg.width) // 2
            y = box[3] - fg.height  # sit on the bottom of the subject zone
            canvas.paste(fg, (x, y), fg)
        else:
            plate = fit_cover(fg.convert("RGB"), (bw, bh))
            canvas.paste(plate, (box[0], box[1]))

    def _place_logo(self, canvas: Image.Image, warnings: list[str]) -> None:
        path = self.profile.logo_path
        if not path or not os.path.isfile(path):
            warnings.append(f"LOGO_NOT_FOUND: {path}")
            return
        logo = Image.open(path).convert("RGBA")
        logo.thumbnail((120, 120), Image.LANCZOS)
        canvas.paste(logo, (THUMBNAIL_WIDTH - 24 - logo.width, 24), logo)

    def _draw_headline(
        self,
        canvas: Image.Image,
        mask: Image.Image,
        display_text: str,
        layout: LayoutTemplate,
        text_box: tuple[int, int, int, int],
        font_path: Optional[str],
        warnings: list[str],
    ) -> tuple[int, tuple[int, int, int, int]]:
        profile = self.profile
        draw = ImageDraw.Draw(canvas)
        mask_draw = ImageDraw.Draw(mask)
        box_w = text_box[2] - text_box[0]
        box_h = text_box[3] - text_box[1]

        # Split a leading number for the big-number layout.
        blocks: list[tuple[str, int, tuple[int, int, int]]] = []  # (text, start_size, colour)
        tokens = display_text.split()
        number_match = _NUMBER_TOKEN.match(tokens[0]) if (layout.big_number and tokens) else None
        if number_match:
            blocks.append((tokens[0], 200, profile.accent_rgb))
            rest = " ".join(tokens[1:])
            if rest:
                blocks.append((rest, layout.start_font_size, profile.text_rgb))
        else:
            start = 128 if layout.big_number else layout.start_font_size
            blocks.append((display_text, start, profile.text_rgb))

        # Fit every block, stacking them vertically inside the box.
        fitted: list[tuple[object, list[str], int, tuple[int, int, int]]] = []
        remaining_h = box_h
        for text, start_size, colour in blocks:
            font, lines, size, fits = fit_text(
                draw, text, font_path, box_w, remaining_h, start_size
            )
            if not fits:
                warnings.append(
                    "TEXT_OVERFLOW: headline does not fully fit the safe area "
                    "at the minimum font size"
                )
            fitted.append((font, lines, size, colour))
            remaining_h -= int(size * 1.08 * len(lines) + 10)
        total_h = sum(int(size * 1.08 * len(lines)) for _, lines, size, _ in fitted) + 10 * (len(fitted) - 1)
        y = text_box[1] + max(0, (box_h - total_h) // 2)

        # Accent bar just above the headline block (exact accent colour --
        # drawn after gradient/vignette so the palette colour is verbatim).
        bar_y = max(0, y - 26)
        draw.rectangle((text_box[0], bar_y, text_box[0] + 180, bar_y + 12), fill=profile.accent_rgb)

        bbox: Optional[list[int]] = None
        main_size = fitted[0][2]
        for font, lines, size, colour in fitted:
            stroke = max(2, round(size / 13))
            shadow_off = max(2, size // 18)
            line_h = size * 1.08
            for line in lines:
                _lb = draw.textbbox((0, 0), line, font=font, stroke_width=stroke)
                line_w = _lb[2] - _lb[0]
                if layout.align == "center":
                    x = text_box[0] + (box_w - line_w) / 2
                else:
                    x = float(text_box[0])
                # shadow, then stroked fill (stroke = high-contrast outline)
                draw.text(
                    (x + shadow_off, y + shadow_off), line, font=font,
                    fill=profile.shadow_rgb, stroke_width=stroke,
                    stroke_fill=profile.shadow_rgb,
                )
                draw.text(
                    (x, y), line, font=font, fill=colour,
                    stroke_width=stroke, stroke_fill=profile.stroke_rgb,
                )
                mask_draw.text(
                    (x, y), line, font=font, fill=255,
                    stroke_width=stroke, stroke_fill=255,
                )
                lb = draw.textbbox((x, y), line, font=font, stroke_width=stroke)
                if bbox is None:
                    bbox = list(lb)
                else:
                    bbox[0] = min(bbox[0], lb[0]); bbox[1] = min(bbox[1], lb[1])
                    bbox[2] = max(bbox[2], lb[2]); bbox[3] = max(bbox[3], lb[3])
                y += line_h
            y += 10

        assert bbox is not None
        return main_size, (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))


def build_thumbnail(
    background: ImageLike,
    headline: str,
    output_path: Union[str, os.PathLike],
    profile: Optional[ChannelStyleProfile] = None,
    layout_id: Optional[str] = None,
    foreground: Optional[ImageLike] = None,
) -> ThumbnailResult:
    """One-shot convenience wrapper around :class:`ThumbnailBuilder`."""
    from .models import default_profile

    builder = ThumbnailBuilder(profile or default_profile())
    return builder.build(
        background, headline, output_path, layout_id=layout_id, foreground=foreground
    )
