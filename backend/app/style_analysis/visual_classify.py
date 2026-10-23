"""Per-scene visual-source classification for Style Lab.

Goal: for each scene of a video, estimate *what kind of visual* it most
likely is — stock footage, AI-generated imagery, raw/camera footage, a
screen capture, a text card, or something else — and aggregate those
per-scene judgements into a percentage ``visual_mix``.

HONESTY IS THE PRODUCT
----------------------
This is a probabilistic estimate, never a determination. Stock footage
and raw camera footage are often visually indistinguishable in a still
frame, and modern AI imagery is designed to look photographic. This
module therefore:

* NEVER claims certainty: confidences are capped below 1.0, no output
  string uses certainty language, and every label is a ``*_like``
  likelihood class, not a factual claim about provenance.
* Labels a scene ``uncertain`` whenever the combined confidence falls
  below :data:`UNCERTAIN_THRESHOLD` (0.55). The best-guess candidate is
  preserved in the scene's ``signals`` as ``candidate:<label>`` so the
  information is not lost, but it does NOT count toward that class in
  the aggregate mix — ``uncertain`` is counted separately.
* Records ``provenance`` per scene: ``"vision+heuristics"`` when the
  Gemini vision judgement contributed, ``"heuristics_only"`` when it
  did not (no API key, API failure / HTTP 429, unparseable response, or
  an injected vision callable returning ``None``). Heuristics-only
  confidences are deliberately discounted and capped (see below), so a
  degraded run visibly looks less sure of itself.

Combination rule (documented contract — tests pin this behaviour)
-----------------------------------------------------------------
1. Heuristics produce ``(h_label, h_conf)`` from cheap frame signals.
   Heuristic confidence alone is weak by design: it is capped at 0.60,
   because still-frame statistics cannot reliably separate stock from
   raw footage or from good AI imagery.
2. When a vision judgement ``(v_label, v_conf)`` is available:
   * labels AGREE  -> ``final_conf = min(0.95, 0.7*v_conf + 0.3*h_conf
     + 0.10)`` (the +0.10 is an agreement bonus), label = agreed label.
   * labels DISAGREE -> label = vision label (the vision model sees
     semantic content the heuristics cannot), but
     ``final_conf = 0.6 * v_conf`` (a disagreement penalty).
3. Heuristics only -> ``final_conf = min(0.55, 0.8 * h_conf)``.
4. In ALL paths: if ``final_conf < UNCERTAIN_THRESHOLD`` the emitted
   label is ``"uncertain"`` and ``final_conf`` is reported as computed
   (it is NOT rounded up to the threshold).

Vision calls use model ``gemini-3.1-flash-lite`` only, go through
``app.providers.http_client.create_async_client`` (the sandbox proxy
breaks raw httpx), and send sampled frames as base64 ``inline_data``
image parts to ``generateContent`` — the same wire pattern as
``app.providers.llm.gemini``. Any failure degrades to heuristics-only;
this module never raises because the vision API is unavailable (it does
raise for a missing/unreadable video file, which is a caller error).
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import subprocess
import tempfile
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence, Union

import numpy as np
from PIL import Image
import io

from ..ffmpeg_resolution import ffmpeg_executable
from ..providers.http_client import create_async_client

__all__ = [
    "CLASSES",
    "UNCERTAIN_THRESHOLD",
    "VISION_MODEL",
    "classify_scenes",
    "aggregate_mix",
    "combine_judgements",
    "frame_signals",
    "heuristics_judgement",
]

#: All output classes. ``uncertain`` is a first-class aggregate bucket.
CLASSES = (
    "stock_like",
    "ai_generated_like",
    "raw_footage_like",
    "screen_capture",
    "text_card",
    "other",
    "uncertain",
)

#: Classes a judgement (vision or heuristic) may propose. ``uncertain``
#: is only ever produced by the threshold rule, never proposed directly.
_CANDIDATE_CLASSES = tuple(c for c in CLASSES if c != "uncertain")

#: Below this combined confidence a scene is labelled ``uncertain``.
UNCERTAIN_THRESHOLD = 0.55

#: The only Gemini model this module will call (larger models 429 on
#: the project key; see module docstring / Round 3 notes).
VISION_MODEL = "gemini-3.1-flash-lite"

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

_FRAME_WIDTH = 320  # downscale target for sampled frames (px)

VisionFn = Callable[
    [list[bytes]], Union[Optional[Mapping[str, Any]], Awaitable[Optional[Mapping[str, Any]]]]
]


# ---------------------------------------------------------------------------
# Scene input normalisation
# ---------------------------------------------------------------------------
def _scene_bounds(scene: Any, index: int) -> tuple[int, float, float]:
    """Accept dicts/objects with start/end (or start_s/end_s/duration)."""
    def get(name: str) -> Any:
        if isinstance(scene, Mapping):
            return scene.get(name)
        return getattr(scene, name, None)

    scene_index = get("scene_index")
    if scene_index is None:
        scene_index = get("index")
    if scene_index is None:
        scene_index = index
    start = get("start")
    if start is None:
        start = get("start_s")
    if start is None:
        start = get("start_time")
    end = get("end")
    if end is None:
        end = get("end_s")
    if end is None:
        end = get("end_time")
    if end is None:
        dur = get("duration")
        if dur is None:
            dur = get("duration_s")
        if dur is not None and start is not None:
            end = float(start) + float(dur)
    if start is None:
        start = 0.0
    if end is None or float(end) <= float(start):
        end = float(start) + 1.0
    return int(scene_index), float(start), float(end)


# ---------------------------------------------------------------------------
# Frame sampling (FFmpeg)
# ---------------------------------------------------------------------------
def extract_frame_bytes(video_path: str, at_seconds: float) -> bytes | None:
    """One downscaled JPEG frame at ``at_seconds``, or None on failure."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        out = tf.name
    try:
        proc = subprocess.run(
            [
                ffmpeg_executable(), "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0.0, at_seconds):.3f}",
                "-i", str(video_path),
                "-frames:v", "1",
                "-vf", f"scale={_FRAME_WIDTH}:-2",
                "-q:v", "5", "-y", out,
            ],
            capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            return None
        with open(out, "rb") as fh:
            data = fh.read()
        return data or None
    except Exception:
        return None
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def sample_scene_frames(
    video_path: str, start: float, end: float, *, max_frames: int = 3
) -> list[bytes]:
    """1–3 frames spread across the scene (start+10%, middle, end-10%)."""
    dur = max(0.2, end - start)
    offsets = [0.15, 0.5, 0.85][:max_frames] if dur >= 1.5 else [0.5]
    frames: list[bytes] = []
    for frac in offsets:
        data = extract_frame_bytes(video_path, start + frac * dur)
        if data:
            frames.append(data)
    return frames


# ---------------------------------------------------------------------------
# Cheap frame signals (pure numpy/PIL — unit-testable without ffmpeg)
# ---------------------------------------------------------------------------
def _laplacian_variance(gray: np.ndarray) -> float:
    g = gray.astype(np.float64)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    )
    return float(lap.var())


def _box_blur(gray: np.ndarray, k: int = 5) -> np.ndarray:
    """Separable moving-average blur (cheap stand-in for a Gaussian)."""
    pad = k // 2
    g = np.pad(gray.astype(np.float64), pad, mode="edge")
    kernel = np.ones(k) / k
    horiz = np.apply_along_axis(lambda r: np.convolve(r, kernel, mode="valid"), 1, g)
    return np.apply_along_axis(lambda c: np.convolve(c, kernel, mode="valid"), 0, horiz)


def frame_signals(image_bytes: bytes) -> dict[str, float]:
    """Cheap still-frame statistics for one JPEG/PNG frame.

    Keys: ``sharpness`` (Laplacian variance), ``noise`` (high-frequency
    residual std), ``palette_breadth`` (distinct quantised colours /
    pixels, 0..1-ish), ``flat_ratio`` (share of pixels in the single
    most common quantised colour), ``edge_density``, ``axis_edge_ratio``
    (share of strong edges that are axis-aligned — UI regularity hint),
    ``luma_std``.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    arr = np.asarray(img).astype(np.float64)
    gray = arr.mean(axis=2)
    h, w = gray.shape

    sharpness = _laplacian_variance(gray)
    noise = float((gray - _box_blur(gray)).std())
    luma_std = float(gray.std())

    # Quantised palette (4 bits/channel) — breadth + dominant-colour share.
    q = (arr // 16).astype(np.int32)
    keys = (q[:, :, 0] << 8) | (q[:, :, 1] << 4) | q[:, :, 2]
    _, counts = np.unique(keys, return_counts=True)
    total = float(keys.size) or 1.0
    palette_breadth = float(len(counts) / total)
    distinct_colors = float(len(counts))
    flat_ratio = float(counts.max() / total) if len(counts) else 0.0

    gx = np.abs(np.diff(gray, axis=1))
    gy = np.abs(np.diff(gray, axis=0))
    strong = 24.0
    edge_density = float(((gx > strong).mean() + (gy > strong).mean()) / 2.0)
    axis = float((gx > strong).sum() + (gy > strong).sum())
    # Diagonal-energy proxy: edges in the residual diagonal direction.
    diag = np.abs(gray[1:, 1:] - gray[:-1, :-1])
    diag_strong = float((diag > strong).sum())
    axis_edge_ratio = axis / (axis + diag_strong) if (axis + diag_strong) > 0 else 0.0

    return {
        "sharpness": round(sharpness, 3),
        "noise": round(noise, 3),
        "palette_breadth": round(palette_breadth, 5),
        "distinct_colors": distinct_colors,
        "flat_ratio": round(flat_ratio, 4),
        "edge_density": round(edge_density, 4),
        "axis_edge_ratio": round(axis_edge_ratio, 4),
        "luma_std": round(luma_std, 3),
        "width": float(w),
        "height": float(h),
    }


def motion_consistency(frames: Sequence[bytes]) -> float | None:
    """Mean normalised frame-to-frame change (0=static, 1=total change).

    Low values = static or near-static scene (title cards, still AI
    images with slow zoom); moderate values = natural moving footage.
    Returns None with fewer than 2 decodable frames."""
    grays: list[np.ndarray] = []
    for data in frames:
        try:
            img = Image.open(io.BytesIO(data)).convert("L").resize((80, 45))
            grays.append(np.asarray(img).astype(np.float64))
        except Exception:
            return None
    if len(grays) < 2:
        return None
    diffs = [
        float(np.abs(grays[i + 1] - grays[i]).mean()) / 255.0
        for i in range(len(grays) - 1)
    ]
    return round(float(np.mean(diffs)), 4) if diffs else None


# ---------------------------------------------------------------------------
# Heuristic judgement
# ---------------------------------------------------------------------------
def heuristics_judgement(
    signals_list: Sequence[Mapping[str, float]],
    motion: float | None,
) -> tuple[str, float, list[str]]:
    """Rule-based (label, confidence, signal strings) from frame signals.

    Deliberately weak: still-frame statistics separate text cards and
    screen captures reasonably, but CANNOT reliably separate stock from
    raw footage or from polished AI imagery — in that ambiguous natural-
    imagery case this returns a low-confidence ``stock_like`` prior only.
    """
    signals: list[str] = []
    if not signals_list:
        return "other", 0.0, ["no_frames_decodable"]

    flat = float(np.mean([s.get("flat_ratio", 0.0) for s in signals_list]))
    palette = float(np.mean([s.get("palette_breadth", 0.0) for s in signals_list]))
    # Absolute distinct-colour count is the discriminating palette signal:
    # palette_breadth (distinct/pixels) saturates near zero even for rich
    # natural frames, because a 4-bit/channel palette has only 4096 bins.
    distinct = float(np.mean([s.get("distinct_colors", 0.0) for s in signals_list]))
    edge = float(np.mean([s.get("edge_density", 0.0) for s in signals_list]))
    axis = float(np.mean([s.get("axis_edge_ratio", 0.0) for s in signals_list]))
    sharp = float(np.mean([s.get("sharpness", 0.0) for s in signals_list]))
    noise = float(np.mean([s.get("noise", 0.0) for s in signals_list]))

    signals.append(f"flat_ratio={flat:.2f}")
    signals.append(f"palette_breadth={palette:.3f}")
    signals.append(f"distinct_colors={distinct:.0f}")
    signals.append(f"edge_density={edge:.3f}")
    if motion is not None:
        signals.append(f"motion={motion:.3f}")

    # Text card: large flat colour area, narrow palette, near-static.
    if flat >= 0.70 and distinct <= 64:
        conf = 0.58 if (motion is None or motion < 0.02) else 0.50
        signals.append("large_flat_colour_area")
        signals.append("narrow_palette")
        if motion is not None and motion < 0.02:
            signals.append("near_static_scene")
        return "text_card", conf, signals

    # Screen capture: regular axis-aligned structure + flat regions.
    if axis >= 0.62 and flat >= 0.25 and edge >= 0.02:
        signals.append("axis_aligned_edge_structure")
        signals.append("ui_like_regularity")
        return "screen_capture", 0.50, signals

    # Natural photographic imagery: heuristics cannot separate stock /
    # raw / AI — emit only a weak prior, which the threshold rule will
    # normally demote to ``uncertain`` without vision support.
    if distinct >= 150 and flat < 0.70:
        signals.append("natural_imagery_statistics")
        signals.append("heuristics_cannot_separate_stock_raw_ai")
        conf = 0.38
        if noise > 6.0 and sharp > 80.0:
            conf = 0.42  # sensor-noise-like texture: still only a prior
            signals.append("sensor_noise_like_texture")
        return "stock_like", conf, signals

    signals.append("no_discriminating_heuristic")
    return "other", 0.30, signals


# ---------------------------------------------------------------------------
# Combination rule (see module docstring — this is the contract)
# ---------------------------------------------------------------------------
def combine_judgements(
    h_label: str,
    h_conf: float,
    vision: Optional[Mapping[str, Any]],
    h_signals: Sequence[str] = (),
) -> dict[str, Any]:
    """Combine heuristic + optional vision judgements into a final label.

    Returns ``{label, confidence, candidate_label, provenance, signals}``
    with the threshold rule applied: ``confidence < UNCERTAIN_THRESHOLD``
    yields label ``"uncertain"`` and the candidate kept in signals.
    """
    signals: list[str] = list(h_signals)
    v_label: str | None = None
    v_conf = 0.0
    if vision:
        raw_label = str(vision.get("label", "")).strip()
        if raw_label in _CANDIDATE_CLASSES:
            v_label = raw_label
            try:
                v_conf = float(vision.get("confidence", 0.0))
            except (TypeError, ValueError):
                v_conf = 0.0
            v_conf = min(0.98, max(0.0, v_conf))
            for s in vision.get("signals") or []:
                signals.append(f"vision:{s}")
        else:
            signals.append("vision_label_unrecognised_ignored")

    if v_label is not None:
        provenance = "vision+heuristics"
        if v_label == h_label:
            final_conf = min(0.95, 0.7 * v_conf + 0.3 * h_conf + 0.10)
            signals.append("vision_and_heuristics_agree")
            candidate = v_label
        else:
            final_conf = 0.6 * v_conf
            signals.append(f"vision_heuristics_disagree:heuristics={h_label}")
            candidate = v_label
    else:
        provenance = "heuristics_only"
        final_conf = min(0.55, 0.8 * h_conf)
        candidate = h_label

    final_conf = round(float(final_conf), 3)
    if final_conf < UNCERTAIN_THRESHOLD:
        signals.append(f"candidate:{candidate}")
        signals.append("below_confidence_threshold")
        label = "uncertain"
    else:
        label = candidate
    return {
        "label": label,
        "confidence": final_conf,
        "candidate_label": candidate,
        "provenance": provenance,
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# Gemini vision judgement
# ---------------------------------------------------------------------------
_VISION_PROMPT = """You are classifying the likely visual SOURCE of one scene from a video, from the sampled frames provided.

Choose exactly ONE best class:
- stock_like: professionally shot/graded footage or photos of the kind sold by stock libraries
- ai_generated_like: imagery that appears synthetically generated (AI image/video)
- raw_footage_like: unpolished camera/phone footage, vlog-style or amateur recording
- screen_capture: a recording of a screen, app, website, game UI or software interface
- text_card: mostly text, a title card, or a plain graphic card
- other: none of the above fits

IMPORTANT — be honest about ambiguity:
- Stock footage and raw footage are visually SIMILAR. From still frames alone they often cannot be told apart reliably. When you are unsure between classes, you MUST lower your confidence (below 0.55 means "I cannot tell").
- Do not claim certainty. Confidence must reflect how discriminating the visible evidence really is.
- List only signals you can actually observe in the frames (e.g. watermark-like artefacts, warped text, extra fingers, unnaturally smooth skin, sensor noise, shallow depth of field, UI chrome, letterboxing, flat background colour).

Respond with ONLY a JSON object, no markdown:
{"label": "<one class>", "confidence": <0.0-1.0>, "signals": ["<observable signal>", ...]}
"""


async def _gemini_vision_judge(
    frames: list[bytes],
    api_key: str,
    *,
    transport: Any = None,
    timeout_s: float = 60.0,
) -> Optional[dict[str, Any]]:
    """One Gemini vision call; returns the parsed judgement or None.

    None on ANY failure (HTTP error incl. 429, timeout, unparseable
    body) — the caller degrades to heuristics-only. Never raises.
    """
    if not frames or not api_key:
        return None
    parts: list[dict[str, Any]] = [{"text": _VISION_PROMPT}]
    for data in frames:
        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(data).decode("ascii"),
                }
            }
        )
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
        },
    }
    url = f"{_GEMINI_BASE}/models/{VISION_MODEL}:generateContent"
    try:
        async with create_async_client(
            transport=transport, timeout=timeout_s
        ) as client:
            resp = await client.post(
                url,
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return None
        cparts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in cparts).strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return None
        if str(parsed.get("label", "")) not in _CANDIDATE_CLASSES:
            return None
        return parsed
    except Exception:
        return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def _classify_async(
    video_path: str,
    scenes: Sequence[Any],
    *,
    api_key: str | None,
    vision_fn: VisionFn | None,
    transport: Any,
    max_frames_per_scene: int,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for i, scene in enumerate(scenes):
        scene_index, start, end = _scene_bounds(scene, i)
        frames = sample_scene_frames(
            video_path, start, end, max_frames=max_frames_per_scene
        )
        if not frames:
            results.append(
                {
                    "scene_index": scene_index,
                    "label": "uncertain",
                    "confidence": 0.0,
                    "candidate_label": "other",
                    "provenance": "heuristics_only",
                    "signals": ["frame_extraction_failed"],
                }
            )
            continue

        sig_list: list[dict[str, float]] = []
        for data in frames:
            try:
                sig_list.append(frame_signals(data))
            except Exception:
                continue
        motion = motion_consistency(frames)
        h_label, h_conf, h_signals = heuristics_judgement(sig_list, motion)

        vision: Optional[Mapping[str, Any]] = None
        if vision_fn is not None:
            try:
                vision = await _maybe_await(vision_fn(frames))
            except Exception:
                vision = None
        elif api_key:
            vision = await _gemini_vision_judge(frames, api_key, transport=transport)

        combined = combine_judgements(h_label, h_conf, vision, h_signals)
        results.append({"scene_index": scene_index, **combined})

    return {
        "scenes": results,
        "visual_mix": aggregate_mix(results),
        "scene_count": len(results),
    }


def aggregate_mix(scene_results: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Percentage share (0–100, 1 decimal) of each class over scenes.

    Every class in :data:`CLASSES` is present (0.0 when absent) so
    consumers never KeyError; ``uncertain`` is counted separately and
    never folded into another class. Empty input → all zeros."""
    counts = {c: 0 for c in CLASSES}
    for r in scene_results:
        label = str(r.get("label", "uncertain"))
        counts[label if label in counts else "uncertain"] += 1
    total = len(scene_results)
    if total == 0:
        return {c: 0.0 for c in CLASSES}
    return {c: round(100.0 * counts[c] / total, 1) for c in CLASSES}


def classify_scenes(
    video_path: str | os.PathLike,
    scenes: Sequence[Any],
    *,
    api_key: str | None = None,
    vision_fn: VisionFn | None = None,
    transport: Any = None,
    max_frames_per_scene: int = 3,
) -> dict[str, Any]:
    """Classify each scene's likely visual source.

    Parameters
    ----------
    video_path: path to the video file (must exist).
    scenes: sequence of scene specs — mappings/objects with ``start`` /
        ``end`` seconds (``start_s``/``end_s``/``duration`` also
        accepted) and optional ``scene_index``.
    api_key: Gemini API key. When omitted, read from the
        ``GEMINI_API_KEY`` environment variable. When no key and no
        ``vision_fn`` are available, classification degrades to
        heuristics-only (lowered confidences, provenance recorded).
    vision_fn: optional injected vision callable (tests / alternative
        providers). Receives the sampled JPEG frame bytes for one scene
        and returns ``{"label", "confidence", "signals"}`` or None; may
        be sync or async. Takes precedence over the Gemini path.

    Returns ``{"scenes": [{scene_index, label, confidence, signals,
    candidate_label, provenance}], "visual_mix": {class: pct},
    "scene_count": int}``. Labels are likelihood estimates, never
    provenance facts; sub-threshold scenes are ``"uncertain"``.
    """
    path = str(video_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"video not found: {path}")
    key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")
    return asyncio.run(
        _classify_async(
            path,
            list(scenes),
            api_key=key,
            vision_fn=vision_fn,
            transport=transport,
            max_frames_per_scene=max_frames_per_scene,
        )
    )
