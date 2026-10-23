"""Style Lab — thumbnail analysis (style, never copy).

``analyse_thumbnail(image_path)`` describes HOW a thumbnail is built, not
WHAT it says. The result object carries only structural/style facts:

* ``text_word_count``   — how many words of on-image text there are
  (a COUNT from the vision model, never the words themselves),
* ``text_role_notes``   — the structural ROLE of that text, coerced to a
  small canonical vocabulary (``"curiosity question"``,
  ``"number-led claim"``, ``"single keyword"``, …),
* ``face_present``      — ``True`` / ``False`` / ``"unknown"`` (vision),
  plus ``face_size_class`` (``"small"`` / ``"medium"`` / ``"large"``),
* ``dominant_colours``  — Pillow palette: top colours as hex + coverage %,
* ``layout_class``      — computed ESTIMATE, one of ``text_heavy`` |
  ``face_led`` | ``object_led`` | ``mixed`` | ``minimal``,
* ``contrast_class``    — computed: ``high`` | ``medium`` | ``low``.

Hard boundary (competitor analysis, style-level only): the actual words
on a competitor's thumbnail are NEVER stored. The vision prompt forbids
transcription, the response parser drops every field except count /
role / face facts, and the role string is coerced to the canonical
vocabulary — so even a misbehaving model response cannot leak copied
text into the result. Vision failure is honest: ``text_word_count`` is
``None`` and ``face_present`` is ``"unknown"`` with the failure recorded
in ``provenance`` — never a guess.

Vision calls go to Gemini ``gemini-3.1-flash-lite`` only, over the
shared provider HTTP factory (:mod:`app.providers.http_client`, the
same pattern as ``app.providers.llm.gemini``: ``x-goog-api-key`` header,
``:generateContent`` endpoint). The API key is read from the
``GEMINI_API_KEY`` environment variable (or the ``api_key`` argument)
just-in-time and is never logged, returned, or stored.

Palette / layout / contrast are deterministic Pillow computations —
no model involved — so they work (and are unit-testable) offline.
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image

from ..providers.http_client import create_async_client, create_client

__all__ = [
    "DEFAULT_MODEL",
    "LAYOUT_CLASSES",
    "CONTRAST_CLASSES",
    "analyse_thumbnail",
    "analyse_thumbnail_async",
    "extract_dominant_colours",
    "compute_image_stats",
    "classify_layout",
    "parse_vision_payload",
    "VISION_PROMPT",
]

#: The ONLY vision model this module may call (assignment constraint).
DEFAULT_MODEL = "gemini-3.1-flash-lite"

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

LAYOUT_CLASSES = ("text_heavy", "face_led", "object_led", "mixed", "minimal")
CONTRAST_CLASSES = ("high", "medium", "low")

_FACE_SIZE_CLASSES = ("small", "medium", "large")

# ---------------------------------------------------------------------------
# Vision prompt — count + structure ONLY. Transcription is forbidden, and
# the parser below independently enforces that (defence in depth).
# ---------------------------------------------------------------------------
VISION_PROMPT = (
    "You are analysing the STYLE of a YouTube thumbnail image. "
    "Do NOT transcribe, quote, or reproduce any of the words visible in "
    "the image — the actual text content must never appear in your "
    "answer. Report only structural facts as a single JSON object with "
    "exactly these keys:\n"
    '{"text_word_count": <integer: how many words of text appear on the '
    'thumbnail, 0 if none>, '
    '"text_role": <one of: "curiosity question", "number-led claim", '
    '"single keyword", "statement claim", "call to action", "no text", '
    '"other" — the structural role of the text, never its content>, '
    '"face_present": <true if a human face is clearly visible, else false>, '
    '"face_size_class": <"small", "medium", or "large" — approximate size '
    "of the face relative to the frame; null when no face is present>}\n"
    "Reply with the JSON object only."
)


# ---------------------------------------------------------------------------
# Vision payload sanitisation — the no-copy guarantee lives here.
# ---------------------------------------------------------------------------
def _canonical_role(raw: Any, word_count: Optional[int]) -> Optional[str]:
    """Coerce a model-supplied role string to the canonical vocabulary.

    Anything unrecognised becomes ``"other"``; a zero word count is
    always ``"no text"``. Because the output is one of a fixed label
    set, no fragment of the thumbnail's actual wording can pass
    through this function.
    """
    if word_count == 0:
        return "no text"
    if not isinstance(raw, str) or not raw.strip():
        return "other" if word_count else None
    r = raw.strip().lower().replace("_", " ").replace("-", " ")
    if "no text" in r or r == "none":
        return "no text"
    if "curiosity" in r:
        return "curiosity question"
    if "number" in r or "numeric" in r or "stat" in r or "digit" in r:
        return "number-led claim"
    if "single" in r or "keyword" in r or "one word" in r:
        return "single keyword"
    if "call to action" in r or r.startswith("cta"):
        return "call to action"
    if "question" in r:
        return "curiosity question"
    if "statement" in r or "claim" in r or "declarative" in r:
        return "statement claim"
    if "list" in r:
        return "number-led claim"
    return "other"


def parse_vision_payload(raw: Any) -> dict:
    """Validate + sanitise a raw vision payload.

    Returns ``{text_word_count, text_role_notes, face_present,
    face_size_class}``. EVERY other key a model might return (e.g. a
    transcription it was told not to produce) is discarded here.
    Invalid/missing fields degrade to ``None`` / ``"unknown"`` rather
    than raising, so callers can treat a garbled reply like a failure.
    """
    if not isinstance(raw, dict):
        return {
            "text_word_count": None,
            "text_role_notes": None,
            "face_present": "unknown",
            "face_size_class": None,
        }
    count = raw.get("text_word_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        # A float that is integral (JSON round-trip) is acceptable.
        if isinstance(count, float) and count.is_integer() and count >= 0:
            count = int(count)
        else:
            count = None
    role = _canonical_role(raw.get("text_role", raw.get("text_role_notes")), count)

    face = raw.get("face_present")
    if face is True:
        face_present: Any = True
    elif face is False:
        face_present = False
    elif isinstance(face, str) and face.strip().lower() in ("true", "present", "yes"):
        face_present = True
    elif isinstance(face, str) and face.strip().lower() in ("false", "absent", "no"):
        face_present = False
    else:
        face_present = "unknown"

    size = raw.get("face_size_class")
    if face_present is not True:
        size = None
    elif not (isinstance(size, str) and size.strip().lower() in _FACE_SIZE_CLASSES):
        size = "medium"  # face seen but size unreported: neutral class
    else:
        size = size.strip().lower()

    return {
        "text_word_count": count,
        "text_role_notes": role if count is not None else None,
        "face_present": face_present,
        "face_size_class": size,
    }


def _vision_failure(reason: str) -> dict:
    """The honest-failure vision result: nulls + 'unknown', no guesses."""
    return {
        "text_word_count": None,
        "text_role_notes": None,
        "face_present": "unknown",
        "face_size_class": None,
        "_provenance": f"unavailable: {reason}",
    }


# ---------------------------------------------------------------------------
# Gemini vision transport (sync + async twins, shared-factory HTTP only)
# ---------------------------------------------------------------------------
def _image_payload(image_path: Path) -> tuple[str, str]:
    """(mime_type, base64) for the vision call.

    Small files go as-is; very large ones are re-encoded at <=1024 px
    wide JPEG so the request stays sane. Style facts do not need the
    full-resolution bytes.
    """
    suffix = image_path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix)
    raw = image_path.read_bytes()
    if mime is not None and len(raw) <= 3_500_000:
        return mime, base64.b64encode(raw).decode("ascii")
    with Image.open(image_path) as img:
        rgb = img.convert("RGB")
        if rgb.width > 1024:
            rgb = rgb.resize(
                (1024, max(1, round(rgb.height * 1024 / rgb.width))),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=85)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")


def _gemini_body(image_path: Path) -> dict:
    mime, b64 = _image_payload(image_path)
    return {
        "contents": [
            {
                "parts": [
                    {"text": VISION_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
        },
    }


def _extract_vision_json(data: dict) -> dict:
    """Pull the JSON object out of a generateContent response."""
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("gemini returned no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("gemini vision reply was not a JSON object")
    return parsed


def _call_vision_sync(
    image_path: Path, *, api_key: str, model: str, timeout_s: float
) -> dict:
    url = f"{_GEMINI_BASE}/models/{model}:generateContent"
    with create_client(timeout=timeout_s) as client:
        resp = client.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=_gemini_body(image_path),
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"gemini HTTP {resp.status_code}")
    return _extract_vision_json(resp.json())


async def _call_vision_async(
    image_path: Path, *, api_key: str, model: str, timeout_s: float
) -> dict:
    url = f"{_GEMINI_BASE}/models/{model}:generateContent"
    async with create_async_client(timeout=timeout_s) as client:
        resp = await client.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=_gemini_body(image_path),
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"gemini HTTP {resp.status_code}")
    return _extract_vision_json(resp.json())


def _resolve_vision_sync(
    image_path: Path,
    *,
    api_key: Optional[str],
    vision_fn: Optional[Callable[..., dict]],
    model: str,
    timeout_s: float,
) -> dict:
    if vision_fn is not None:
        try:
            return {**parse_vision_payload(vision_fn(image_path)), "_provenance": "injected vision_fn"}
        except Exception as exc:  # noqa: BLE001 - degrade, never guess
            return _vision_failure(f"vision_fn raised {type(exc).__name__}")
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        return _vision_failure("no GEMINI_API_KEY available")
    try:
        raw = _call_vision_sync(image_path, api_key=key, model=model, timeout_s=timeout_s)
        return {**parse_vision_payload(raw), "_provenance": f"gemini:{model}"}
    except Exception as exc:  # noqa: BLE001 - degrade, never guess
        return _vision_failure(f"gemini call failed ({type(exc).__name__}: {exc})"[:160])


async def _resolve_vision_async(
    image_path: Path,
    *,
    api_key: Optional[str],
    vision_fn: Optional[Callable[..., dict]],
    model: str,
    timeout_s: float,
) -> dict:
    if vision_fn is not None:
        try:
            out = vision_fn(image_path)
            import inspect

            if inspect.isawaitable(out):
                out = await out
            return {**parse_vision_payload(out), "_provenance": "injected vision_fn"}
        except Exception as exc:  # noqa: BLE001
            return _vision_failure(f"vision_fn raised {type(exc).__name__}")
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        return _vision_failure("no GEMINI_API_KEY available")
    try:
        raw = await _call_vision_async(
            image_path, api_key=key, model=model, timeout_s=timeout_s
        )
        return {**parse_vision_payload(raw), "_provenance": f"gemini:{model}"}
    except Exception as exc:  # noqa: BLE001
        return _vision_failure(f"gemini call failed ({type(exc).__name__}: {exc})"[:160])


# ---------------------------------------------------------------------------
# Pillow palette — deterministic, no model.
# ---------------------------------------------------------------------------
def extract_dominant_colours(
    image: Image.Image, max_colours: int = 5
) -> list[dict]:
    """Top ``max_colours`` colours as ``{hex, rgb, coverage_pct}``.

    Method: downscale to <=320x180 (LANCZOS), median-cut quantise to 8
    colours with no dithering, count exact palette pixels. Same input
    => same output (Pillow's median-cut is deterministic).
    """
    rgb = image.convert("RGB")
    if rgb.width > 320 or rgb.height > 180:
        rgb = rgb.copy()
        rgb.thumbnail((320, 180), Image.LANCZOS)
    total = rgb.width * rgb.height
    if total == 0:
        return []
    quantised = rgb.quantize(colors=8, method=Image.MEDIANCUT, dither=Image.NONE)
    palette = quantised.getpalette() or []
    counts = sorted(quantised.getcolors() or [], key=lambda t: (-t[0], t[1]))
    out: list[dict] = []
    for count, idx in counts[:max_colours]:
        r, g, b = palette[idx * 3 : idx * 3 + 3]
        out.append(
            {
                "hex": f"#{r:02X}{g:02X}{b:02X}",
                "rgb": [r, g, b],
                "coverage_pct": round(100.0 * count / total, 2),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Computed image statistics — the signals behind layout/contrast classes.
# ---------------------------------------------------------------------------
_EDGE_THRESHOLD = 28  # luminance gradient counted as an edge (as legibility.py)
_STATS_SIZE = (256, 144)


def _percentile(sorted_vals: list[int], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(pct * (len(sorted_vals) - 1)))))
    return float(sorted_vals[idx])


def compute_image_stats(image: Image.Image) -> dict:
    """Deterministic signals (all plain numbers — no image content kept).

    * ``edge_density`` — fraction of neighbour pairs (at 256x144) whose
      luminance gradient exceeds the edge threshold.
    * ``luminance_*`` — mean / p5 / p95 of luminance;
      ``contrast_spread`` = (p95 - p5) / 255.
    * ``grid_edge_max`` / ``grid_edge_mean`` — edge density over a
      4x3 grid; the max cell is the busiest region (text/subject area).
    * ``text_region_contrast`` — luminance spread inside that busiest
      cell (a proxy for text-vs-background contrast when text is the
      busiest thing in frame; recorded as a signal either way).
    * ``blockiness`` — fraction of horizontally/vertically adjacent
      cells in a 32x18 quantised grid sharing a colour: high for flat
      colour-block designs, low for photographic detail.
    * ``saturation_mean`` — mean HSV saturation (0..1).
    """
    grey = image.convert("L").resize(_STATS_SIZE, Image.LANCZOS)
    w, h = _STATS_SIZE
    px = grey.load()
    values = sorted(grey.getdata())
    n = len(values)
    mean = sum(values) / n if n else 0.0
    p5, p95 = _percentile(values, 0.05), _percentile(values, 0.95)
    spread = (p95 - p5) / 255.0

    def _cell_edges(x0: int, y0: int, x1: int, y1: int) -> float:
        edges = total = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                p = px[x, y]
                if x + 1 < x1:
                    total += 1
                    edges += abs(p - px[x + 1, y]) > _EDGE_THRESHOLD
                if y + 1 < y1:
                    total += 1
                    edges += abs(p - px[x, y + 1]) > _EDGE_THRESHOLD
        return edges / total if total else 0.0

    edge_density = _cell_edges(0, 0, w, h)

    cols, rows = 4, 3
    cell_edges: list[float] = []
    cell_spreads: list[float] = []
    for r in range(rows):
        for c in range(cols):
            x0, x1 = c * w // cols, (c + 1) * w // cols
            y0, y1 = r * h // rows, (r + 1) * h // rows
            cell_edges.append(_cell_edges(x0, y0, x1, y1))
            cell_vals = sorted(
                px[x, y] for y in range(y0, y1) for x in range(x0, x1)
            )
            cell_spreads.append(
                (_percentile(cell_vals, 0.95) - _percentile(cell_vals, 0.05)) / 255.0
            )
    busiest = max(range(len(cell_edges)), key=lambda i: cell_edges[i]) if cell_edges else 0

    # Blockiness on a small quantised grid.
    small_q = (
        image.convert("RGB")
        .resize((32, 18), Image.LANCZOS)
        .quantize(colors=16, method=Image.MEDIANCUT, dither=Image.NONE)
    )
    grid = list(small_q.getdata())
    gw, gh = 32, 18
    same = pairs = 0
    for y in range(gh):
        for x in range(gw):
            v = grid[y * gw + x]
            if x + 1 < gw:
                pairs += 1
                same += grid[y * gw + x + 1] == v
            if y + 1 < gh:
                pairs += 1
                same += grid[(y + 1) * gw + x] == v
    blockiness = same / pairs if pairs else 0.0

    hsv = image.convert("RGB").resize((64, 36), Image.LANCZOS).convert("HSV")
    sat_mean = sum(p[1] for p in hsv.getdata()) / max(1, 64 * 36) / 255.0

    return {
        "edge_density": round(edge_density, 4),
        "luminance_mean": round(mean / 255.0, 4),
        "luminance_p5": round(p5 / 255.0, 4),
        "luminance_p95": round(p95 / 255.0, 4),
        "contrast_spread": round(spread, 4),
        "grid_edge_max": round(max(cell_edges), 4) if cell_edges else 0.0,
        "grid_edge_mean": round(sum(cell_edges) / len(cell_edges), 4) if cell_edges else 0.0,
        "text_region_contrast": round(cell_spreads[busiest], 4) if cell_spreads else 0.0,
        "blockiness": round(blockiness, 4),
        "saturation_mean": round(sat_mean, 4),
    }


def classify_layout(
    signals: dict,
    *,
    text_word_count: Optional[int],
    face_present: Any,
    face_size_class: Optional[str],
) -> tuple[str, str]:
    """(layout_class, contrast_class) — a labelled ESTIMATE.

    Deterministic rules over the vision facts + computed signals, in
    priority order: a leading face beats text, heavy text beats scene
    detail, near-flat frames are minimal, busy faceless/textless frames
    are object-led, and everything else is honestly ``mixed``.
    """
    spread = float(signals.get("contrast_spread", 0.0))
    contrast_class = "high" if spread >= 0.55 else "medium" if spread >= 0.30 else "low"

    edge = float(signals.get("edge_density", 0.0))
    grid_max = float(signals.get("grid_edge_max", 0.0))

    if face_present is True and face_size_class in ("medium", "large"):
        return "face_led", contrast_class
    if text_word_count is not None and (
        text_word_count >= 5 or (text_word_count >= 3 and grid_max >= 0.25)
    ):
        return "text_heavy", contrast_class
    if edge <= 0.02 and spread <= 0.30 and float(signals.get("blockiness", 0.0)) >= 0.7:
        return "minimal", contrast_class
    if face_present is True:
        return "mixed", contrast_class  # small face: present, not leading
    if text_word_count:
        return "mixed", contrast_class  # some text + scene: neither dominates
    if edge >= 0.08:
        return "object_led", contrast_class
    if edge <= 0.04 and float(signals.get("blockiness", 0.0)) >= 0.55:
        return "minimal", contrast_class
    return "mixed", contrast_class


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def _assemble(image: Image.Image, vision: dict) -> dict:
    colours = extract_dominant_colours(image)
    signals = compute_image_stats(image)
    layout_class, contrast_class = classify_layout(
        signals,
        text_word_count=vision["text_word_count"],
        face_present=vision["face_present"],
        face_size_class=vision["face_size_class"],
    )
    return {
        # --- schema fields (stylelab thumbnail profile) ---
        "text_word_count": vision["text_word_count"],
        "text_role_notes": vision["text_role_notes"],
        "face_present": vision["face_present"],
        "dominant_colours": colours,
        "layout_class": layout_class,
        "contrast_class": contrast_class,
        # --- supporting detail ---
        "face_size_class": vision["face_size_class"],
        "layout_is_estimate": True,
        "signals": signals,
        "provenance": {
            "vision": vision.get("_provenance", "unknown"),
            "palette": "pillow_mediancut_top5",
            "layout": "computed_estimate_from_signals_and_vision_facts",
        },
    }


def analyse_thumbnail(
    image_path: str | Path,
    *,
    api_key: Optional[str] = None,
    vision_fn: Optional[Callable[..., dict]] = None,
    model: str = DEFAULT_MODEL,
    timeout_s: float = 30.0,
) -> dict:
    """Analyse one thumbnail image's style. See module docstring.

    ``vision_fn`` (tests / alternate transports) receives the image
    path and returns a raw vision payload dict; it is sanitised by
    :func:`parse_vision_payload` exactly like a real model reply.
    Raises ``FileNotFoundError`` for a missing image — that is a caller
    error, unlike a vision failure, which degrades to nulls/unknown.
    """
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"thumbnail image not found: {path}")
    vision = _resolve_vision_sync(
        path, api_key=api_key, vision_fn=vision_fn, model=model, timeout_s=timeout_s
    )
    with Image.open(path) as img:
        return _assemble(img.copy(), vision)


async def analyse_thumbnail_async(
    image_path: str | Path,
    *,
    api_key: Optional[str] = None,
    vision_fn: Optional[Callable[..., Any]] = None,
    model: str = DEFAULT_MODEL,
    timeout_s: float = 30.0,
) -> dict:
    """Async twin of :func:`analyse_thumbnail`."""
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"thumbnail image not found: {path}")
    vision = await _resolve_vision_async(
        path, api_key=api_key, vision_fn=vision_fn, model=model, timeout_s=timeout_s
    )
    with Image.open(path) as img:
        return _assemble(img.copy(), vision)
