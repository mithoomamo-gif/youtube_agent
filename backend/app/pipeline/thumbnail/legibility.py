"""Mobile-legibility check.

YouTube thumbnails are mostly seen tiny (mobile feed). The deterministic
heuristic (roadmap section 21: "downscale-to-10% mobile-legibility test"):

1. Downscale the finished 1280x720 thumbnail to 128x72 (10%).
2. Look only at the headline's text region (bbox, and the builder's text
   mask when available) at that small size.
3. Measure, deterministically (pure Pillow + stdlib statistics):
   * **contrast spread** -- (p95 - p5) of luminance in the region. Bold text
     with a dark stroke over any background spans near-black..near-white;
     low-contrast text on a similar background spans almost nothing.
   * **edge density** -- fraction of region pixels whose horizontal/vertical
     luminance gradient exceeds a threshold. Real letterforms are dense in
     edges; blurred-away tiny text is not.
   * **effective text height** (mask only) -- the mask's height at 10% size.
     A gate: a text block under 5 px tall at 128x72 is unreadable, full stop.
4. Score = weighted blend; hard gates can fail a thumbnail regardless of
   score (contrast spread < 0.30, or mask text height < 5 px at 10%).

PASS requires score >= 0.50 and no gate failure.
"""

from __future__ import annotations

from typing import Optional

from PIL import Image

from .models import LegibilityResult

SMALL_SIZE = (128, 72)
PASS_SCORE = 0.50
MIN_CONTRAST_SPREAD = 0.30
MIN_TEXT_HEIGHT_SMALL = 5  # px at 128x72
_EDGE_THRESHOLD = 28  # luminance gradient (0-255) counted as an edge


def _percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(pct * (len(ordered) - 1)))))
    return float(ordered[idx])


def _scale_box(
    box: tuple[int, int, int, int], sx: float, sy: float, w: int, h: int
) -> tuple[int, int, int, int]:
    x0 = max(0, min(w - 1, int(box[0] * sx)))
    y0 = max(0, min(h - 1, int(box[1] * sy)))
    x1 = max(x0 + 1, min(w, int(round(box[2] * sx))))
    y1 = max(y0 + 1, min(h, int(round(box[3] * sy))))
    return x0, y0, x1, y1


def check_legibility(
    image: Image.Image,
    text_bbox: Optional[tuple[int, int, int, int]] = None,
    text_mask: Optional[Image.Image] = None,
) -> LegibilityResult:
    """Evaluate ``image`` (any size; expected 1280x720).

    ``text_bbox`` restricts measurement to the headline region (full-size
    coordinates). ``text_mask`` (mode "L", same size as ``image``, white =
    text incl. stroke) enables the effective-text-height gate and a coverage
    term. Without either, the whole frame is measured.
    """
    full_w, full_h = image.size
    small = image.convert("L").resize(SMALL_SIZE, Image.LANCZOS)
    sw, sh = SMALL_SIZE
    sx, sy = sw / full_w, sh / full_h

    if text_bbox is not None:
        region_box = _scale_box(text_bbox, sx, sy, sw, sh)
    else:
        region_box = (0, 0, sw, sh)
    region = small.crop(region_box)
    rw, rh = region.size
    pixels = list(region.getdata())

    # --- contrast spread ----------------------------------------------------
    spread = (_percentile(pixels, 0.95) - _percentile(pixels, 0.05)) / 255.0

    # --- edge density -------------------------------------------------------
    edges = 0
    total = 0
    data = region.load()
    for y in range(rh):
        for x in range(rw):
            p = data[x, y]
            if x + 1 < rw:
                total += 1
                if abs(p - data[x + 1, y]) > _EDGE_THRESHOLD:
                    edges += 1
            if y + 1 < rh:
                total += 1
                if abs(p - data[x, y + 1]) > _EDGE_THRESHOLD:
                    edges += 1
    edge_density = (edges / total) if total else 0.0

    reasons: list[str] = []
    gates_failed = False
    if spread < MIN_CONTRAST_SPREAD:
        gates_failed = True
        reasons.append(
            f"contrast spread {spread:.2f} < {MIN_CONTRAST_SPREAD} at 10% size"
        )

    # --- mask terms -----------------------------------------------------------
    coverage: Optional[float] = None
    text_height_small: Optional[int] = None
    if text_mask is not None:
        mask_small = text_mask.convert("L").resize(SMALL_SIZE, Image.LANCZOS)
        mask_region = mask_small.crop(region_box)
        mask_pixels = list(mask_region.getdata())
        if mask_pixels:
            coverage = sum(1 for v in mask_pixels if v > 127) / len(mask_pixels)
        bbox_small = mask_small.getbbox()
        if bbox_small is not None:
            text_height_small = bbox_small[3] - bbox_small[1]
            if text_height_small < MIN_TEXT_HEIGHT_SMALL:
                gates_failed = True
                reasons.append(
                    f"text height {text_height_small}px at 10% size "
                    f"< {MIN_TEXT_HEIGHT_SMALL}px (text too small for mobile)"
                )
        else:
            gates_failed = True
            reasons.append("no text found in mask")

    contrast_component = min(spread / 0.85, 1.0)
    edge_component = min(edge_density / 0.30, 1.0)
    if coverage is not None and text_height_small is not None:
        height_component = min(text_height_small / 12.0, 1.0)
        score = (
            0.35 * contrast_component
            + 0.30 * edge_component
            + 0.35 * height_component
        )
    else:
        score = 0.60 * contrast_component + 0.40 * edge_component

    passed = (score >= PASS_SCORE) and not gates_failed
    if passed:
        reasons.append("legible at 10% (mobile) size")
    elif score < PASS_SCORE:
        reasons.append(f"score {score:.2f} < {PASS_SCORE}")

    return LegibilityResult(
        passed=passed,
        score=round(score, 4),
        checked_size=SMALL_SIZE,
        details={
            "contrast_spread": round(spread, 4),
            "edge_density": round(edge_density, 4),
            "coverage": (round(coverage, 4) if coverage is not None else None),
            "text_height_small_px": text_height_small,
            "region_small": region_box,
        },
        reasons=reasons,
    )
