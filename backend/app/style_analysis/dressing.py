"""Style Lab — audio & editing "dressing" analysis.

Scope (this module only): the dressing layered over a video's core
visuals and speech —

1. **Music presence** — is there likely a music bed under the audio?
2. **Caption burn-in** — are subtitles burned into the picture (as
   opposed to a sidecar / player-rendered track)?
3. **Text-overlay frequency** — how often do editor-added text
   overlays (titles, big keywords, labels) appear, per minute?

Camera/editing *motion* is deliberately NOT assessed here — it belongs
to the scenes stage; the returned :class:`DressingAnalysis` leaves
``motion`` at its uncertain default.

Honesty contract (mirrors ``app.style_analysis.schema``)
--------------------------------------------------
Everything here is an *estimate* and is labelled with a confidence in
[0, 1]; nothing is claimed as certain:

* The music estimator is the weakest of the three. Without source
  separation, music under speech can only be inferred indirectly —
  chiefly from what the audio does *during speech pauses* (a music bed
  keeps playing; a voice-only track falls to near-silence) plus the
  tonal structure of those pauses. A ducked, quiet or noise-like bed
  can be missed, and steady background noise / reverb can imitate one.
  The estimator is therefore deliberately conservative: the middle
  label (``music_possible``) covers a wide band, confidences are
  capped (0.85 at best, 0.6 when only global tonality was usable),
  and every raw signal is returned so a reader can judge the evidence.
* Caption burn-in is judged by ONE batched Gemini-vision call over
  12–20 sampled frames ("does this frame contain burned-in subtitle
  text at the bottom? yes/no/unsure" per frame, plus the same for
  non-caption overlays). ``likely`` requires a documented fraction
  (:data:`CAPTION_YES_FRACTION_LIKELY` = 50%) of frames answering yes.
  If the vision call is unavailable or fails, a cheap heuristic
  (persistent high-contrast structure in the lower third) runs instead
  and the result is marked ``provenance="heuristic_only"`` with
  confidence capped at 0.45 — the heuristic cannot read text, it can
  only notice that *something* text-shaped sits at the bottom.
* Overlay frequency counts *runs* of overlay-positive sampled frames
  as events and divides by the video's minutes. Sparse sampling
  undercounts short overlays, so the figure is a lower-bound estimate
  and says so in its notes; confidence stays in the low/mid range.

Public entry point: :func:`analyse_dressing` (sync; runs the async
core via ``asyncio.run``, exactly like ``visual_classify``). It
returns the shared schema type :class:`app.style_analysis.schema.DressingAnalysis`
whose labels are ``present`` / ``absent`` / ``uncertain``; the
finer-grained estimator vocabularies (``music_likely`` /
``music_possible`` / ``no_music_detected``; caption ``likely`` /
``possible`` / ``unlikely``) live on the sub-estimator functions and
are preserved in the schema ``notes`` so nothing is lost in mapping.

All provider HTTP goes through
``app.providers.http_client.create_async_client`` (the shared factory
that survives the sandbox's bracketed-IPv6 ``NO_PROXY``), and the only
Gemini model used is :data:`VISION_MODEL` (larger models 429 on the
project key).
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence, Union

import numpy as np
from PIL import Image

from ..ffmpeg_resolution import ffmpeg_executable, ffprobe_executable
from ..providers.http_client import create_async_client
from .schema import DressingAnalysis, PresenceAssessment

__all__ = [
    "VISION_MODEL",
    "DEFAULT_FRAME_COUNT",
    "CAPTION_YES_FRACTION_LIKELY",
    "CAPTION_YES_FRACTION_POSSIBLE",
    "DressingError",
    "FrameVerdict",
    "analyse_dressing",
    "analyze_dressing",
    "estimate_music_presence",
    "analyse_music",
    "music_signals",
    "decode_audio_samples",
    "sample_frame_bytes",
    "frame_dressing_signals",
    "heuristic_frame_verdicts",
    "aggregate_caption_verdicts",
    "aggregate_overlay_verdicts",
    "probe_duration_s",
]

#: The only Gemini model this module will call (larger models 429 on
#: the project key; see module docstring / Round 3 notes).
VISION_MODEL = "gemini-3.1-flash-lite"

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

#: Sampled frames per video for the caption/overlay check (task range
#: 12–20). More frames = better overlay coverage, more vision tokens.
DEFAULT_FRAME_COUNT = 16

_FRAME_WIDTH = 640  # downscale target for sampled frames (px)

#: Caption decision rule (documented fraction): at least this share of
#: sampled frames must answer "yes" for a ``likely`` verdict…
CAPTION_YES_FRACTION_LIKELY = 0.50
#: …and at least this share for ``possible``; below it, ``unlikely``.
CAPTION_YES_FRACTION_POSSIBLE = 0.25

#: An injected vision callable receives ALL sampled frame JPEGs at
#: once (one batched judgement) and returns either a mapping
#: ``{"frames": [{"frame": i, "caption": "yes|no|unsure",
#: "overlay": "yes|no|unsure"}, ...]}`` or a bare list of such items;
#: it may be sync or async, and may return None (= unavailable).
VisionFn = Callable[
    [list[bytes]],
    Union[Optional[Any], Awaitable[Optional[Any]]],
]


class DressingError(RuntimeError):
    """Raised when a dressing sub-analysis cannot run at all."""


# ---------------------------------------------------------------------------
# Duration probe (ffprobe — measured fact, no confidence attached)
# ---------------------------------------------------------------------------
def probe_duration_s(video_path: str | os.PathLike, *, ffprobe: str = ffprobe_executable()) -> float | None:
    """Duration in seconds via ffprobe, or None when unprobeable."""
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_format", "-of", "json", str(video_path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        dur = (data.get("format") or {}).get("duration")
        return float(dur) if dur is not None else None
    except Exception:
        return None


def _duration_from_scenes(scenes: Optional[Sequence[Any]]) -> float | None:
    """Best-effort duration from a scenes sequence (dicts or objects)."""
    if not scenes:
        return None
    ends: list[float] = []
    for scene in scenes:
        def get(name: str) -> Any:
            if isinstance(scene, Mapping):
                return scene.get(name)
            return getattr(scene, name, None)

        end = get("end") or get("end_s") or get("end_time")
        if end is None:
            start = get("start") or get("start_s") or 0.0
            dur = get("duration") or get("duration_s")
            if dur is not None:
                end = float(start) + float(dur)
        if end is not None:
            ends.append(float(end))
    return max(ends) if ends else None


# ---------------------------------------------------------------------------
# 1. Music presence — audio-side estimator (FFmpeg decode + numpy only)
# ---------------------------------------------------------------------------
_MUSIC_SAMPLE_RATE = 16000
_FRAME_S = 0.05  # 50 ms analysis frames
_MIN_PAUSE_S = 0.25  # shortest gap that counts as a speech pause
_MIN_TOTAL_PAUSE_S = 0.5  # need at least this much pause material


def decode_audio_samples(
    media_path: str | os.PathLike,
    *,
    sample_rate: int = _MUSIC_SAMPLE_RATE,
    ffmpeg: str = ffmpeg_executable(),
    timeout_s: float = 600.0,
) -> tuple[np.ndarray, int]:
    """Decode a media file's audio to mono float samples via FFmpeg.

    Returns ``(samples, sample_rate)`` with samples in [-1, 1] as
    float64. Raises :class:`DressingError` when there is no decodable
    audio stream (or ffmpeg fails) — callers decide what that means.
    """
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(media_path),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise DressingError(f"ffmpeg audio decode failed: {detail or 'no audio data'}")
    samples = np.frombuffer(proc.stdout, dtype="<f4").astype(np.float64)
    return samples, sample_rate


def _frame_levels(samples: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-50ms-frame (dBFS, RMS) arrays."""
    frame_len = max(1, int(sample_rate * _FRAME_S))
    count = len(samples) // frame_len
    if count == 0:
        return np.zeros(0), np.zeros(0)
    frames = samples[: count * frame_len].reshape(count, frame_len)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    db = 20.0 * np.log10(rms + 1e-9)
    return db, rms


def _spectrum_stats(segment: np.ndarray, sample_rate: int) -> dict[str, float | None]:
    """Spectral flatness + peakiness of an audio segment.

    Music beds are usually *tonal*: energy concentrated in harmonic
    peaks (low flatness, high max/median ratio). Silence and broadband
    noise are not. Returns None values when the segment is too short
    or effectively silent (no spectrum worth describing).
    """
    if len(segment) < 8192 or float(np.sqrt(np.mean(segment**2))) < 1e-4:
        return {"spectral_flatness": None, "peak_ratio": None}
    win = 8192
    window = np.hanning(win)
    mags: list[np.ndarray] = []
    step = win // 2
    usable = min(len(segment), sample_rate * 30)
    for start in range(0, max(1, usable - win + 1), step):
        chunk = segment[start : start + win]
        if len(chunk) < win:
            break
        mags.append(np.abs(np.fft.rfft(chunk * window)))
        if len(mags) >= 24:
            break
    if not mags:
        return {"spectral_flatness": None, "peak_ratio": None}
    mag = np.mean(mags, axis=0)
    freqs = np.fft.rfftfreq(win, d=1.0 / sample_rate)
    band = (freqs >= 60.0) & (freqs <= min(6000.0, sample_rate / 2.0))
    m = mag[band] + 1e-12
    flatness = float(np.exp(np.mean(np.log(m))) / np.mean(m))
    peak_ratio = float(np.max(m) / np.median(m))
    return {"spectral_flatness": flatness, "peak_ratio": peak_ratio}


def _pause_runs(speech_mask: np.ndarray) -> list[tuple[int, int]]:
    """Interior non-speech runs (frame indices [start, end)) flanked by
    speech on BOTH sides and at least ``_MIN_PAUSE_S`` long."""
    min_len = max(1, int(round(_MIN_PAUSE_S / _FRAME_S)))
    runs: list[tuple[int, int]] = []
    n = len(speech_mask)
    i = 0
    while i < n:
        if speech_mask[i]:
            i += 1
            continue
        j = i
        while j < n and not speech_mask[j]:
            j += 1
        if i > 0 and j < n and (j - i) >= min_len:
            runs.append((i, j))
        i = j
    return runs


def music_signals(samples: np.ndarray, sample_rate: int) -> dict[str, Any]:
    """Raw music-presence signals + score from decoded samples.

    Returns a dict with the estimator ``label`` (``music_likely`` /
    ``music_possible`` / ``no_music_detected``), a capped
    ``confidence``, the 0–1 ``music_score`` the label was derived
    from, the ``method`` actually used (``pause_analysis`` /
    ``global_tonality_only`` / ``silence``) and the individual signal
    values. Exposed separately from file decoding so it is unit
    testable on synthetic sample arrays.
    """
    duration_s = len(samples) / float(sample_rate) if sample_rate else 0.0
    signals: dict[str, Any] = {"duration_s": round(duration_s, 3)}
    db, rms = _frame_levels(samples, sample_rate)
    if len(db) == 0 or duration_s < 0.5:
        return {
            "label": "no_music_detected", "confidence": 0.0,
            "music_score": 0.0, "method": "too_short",
            "signals": {**signals, "error": "audio shorter than 0.5 s"},
        }
    peak_rms = float(np.max(rms))
    signals["peak_dbfs"] = round(float(np.max(db)), 2)
    if peak_rms < 3e-4:  # ~ -70 dBFS: digital silence, no music in it
        return {
            "label": "no_music_detected", "confidence": 0.8,
            "music_score": 0.0, "method": "silence",
            "signals": {**signals, "silence": True},
        }

    floor_db = float(np.percentile(db, 10))
    speech_ref_db = float(np.percentile(db, 95))
    dynamic_db = speech_ref_db - floor_db
    signals.update({
        "floor_dbfs": round(floor_db, 2),
        "speech_ref_dbfs": round(speech_ref_db, 2),
        "dynamic_range_db": round(dynamic_db, 2),
    })
    threshold = floor_db + max(9.0, 0.5 * dynamic_db)
    speech_mask = db > threshold
    signals["speech_frame_fraction"] = round(float(np.mean(speech_mask)), 3)

    pauses = _pause_runs(speech_mask)
    total_pause_s = sum(e - s for s, e in pauses) * _FRAME_S
    signals["pause_count"] = len(pauses)
    signals["pause_total_s"] = round(total_pause_s, 3)

    score: float
    if pauses and total_pause_s >= _MIN_TOTAL_PAUSE_S and dynamic_db >= 10.0:
        method = "pause_analysis"
        frame_len = int(sample_rate * _FRAME_S)
        pause_idx = np.concatenate(
            [np.arange(s, e) for s, e in pauses]
        )
        pause_db = db[pause_idx]
        speech_db_mean = float(np.mean(db[speech_mask]))
        pause_db_mean = float(np.mean(pause_db))
        fill_db = speech_db_mean - pause_db_mean
        # Concatenate the pause audio (capped) for spectral analysis.
        chunks = [
            samples[s * frame_len : e * frame_len] for s, e in pauses
        ]
        pause_audio = np.concatenate(chunks)[: sample_rate * 45]
        spec = _spectrum_stats(pause_audio, sample_rate)
        pause_std = float(np.std(pause_db))
        signals.update({
            "pause_fill_db": round(fill_db, 2),
            "pause_level_std_db": round(pause_std, 2),
            "pause_spectral_flatness": (
                round(spec["spectral_flatness"], 4)
                if spec["spectral_flatness"] is not None else None
            ),
            "pause_peak_ratio": (
                round(spec["peak_ratio"], 2)
                if spec["peak_ratio"] is not None else None
            ),
        })
        # Component scores, each 0..1 (see module docstring honesty):
        # * fill: a bed 34+ dB under speech is inaudible/absent; a bed
        #   ~16 dB under speech is a clearly audible underscore.
        s_fill = float(np.clip((34.0 - fill_db) / 18.0, 0.0, 1.0))
        if pause_db_mean < -60.0:
            # Pauses are effectively silent: nothing tonal to find and
            # steadiness of nothing is not evidence of music.
            s_tonal, s_steady = 0.0, 0.0
        else:
            peak = spec["peak_ratio"]
            s_tonal = (
                float(np.clip((np.log10(peak) - 0.3) / 1.2, 0.0, 1.0))
                if peak else 0.3
            )
            s_steady = float(np.clip(1.0 - pause_std / 9.0, 0.0, 1.0))
        signals.update({
            "score_fill": round(s_fill, 3),
            "score_tonal": round(s_tonal, 3),
            "score_steady": round(s_steady, 3),
        })
        score = 0.50 * s_fill + 0.30 * s_tonal + 0.20 * s_steady
    else:
        # No usable pauses (continuous speech, music-only, or a very
        # flat mix): only global tonality remains — weak evidence,
        # confidence is capped below accordingly.
        method = "global_tonality_only"
        spec = _spectrum_stats(samples[: sample_rate * 60], sample_rate)
        flat = spec["spectral_flatness"]
        peak = spec["peak_ratio"]
        signals.update({
            "global_spectral_flatness": (
                round(flat, 4) if flat is not None else None
            ),
            "global_peak_ratio": (
                round(peak, 2) if peak is not None else None
            ),
        })
        s_tonal = (
            float(np.clip((np.log10(peak) - 0.3) / 1.2, 0.0, 1.0))
            if peak else 0.3
        )
        s_flat = (
            float(np.clip((0.55 - flat) / 0.5, 0.0, 1.0))
            if flat is not None else 0.3
        )
        score = 0.6 * s_tonal + 0.4 * s_flat

    signals["music_score"] = round(float(score), 3)
    if score >= 0.62:
        label = "music_likely"
        confidence = min(0.85, 0.50 + (score - 0.62) * 0.9)
    elif score <= 0.38:
        label = "no_music_detected"
        confidence = min(0.90, 0.50 + (0.38 - score) * 1.0)
    else:
        label = "music_possible"
        confidence = 0.35
    if method == "global_tonality_only":
        confidence = min(confidence, 0.60)
        if label == "music_possible":
            confidence = 0.30
    return {
        "label": label,
        "confidence": round(float(confidence), 3),
        "music_score": round(float(score), 3),
        "method": method,
        "signals": signals,
    }


def estimate_music_presence(
    media_path: str | os.PathLike, *, ffmpeg: str = ffmpeg_executable()
) -> dict[str, Any]:
    """Estimate music presence for a media file (audio or video).

    Returns :func:`music_signals`' dict. When the audio cannot be
    decoded at all, returns ``no_music_detected`` with confidence 0.0
    (="not assessed", see ``signals.error``) rather than raising, so
    batch callers degrade gracefully.
    """
    try:
        samples, sr = decode_audio_samples(media_path, ffmpeg=ffmpeg)
    except Exception as exc:  # DressingError or ffmpeg missing, etc.
        return {
            "label": "no_music_detected", "confidence": 0.0,
            "music_score": 0.0, "method": "decode_failed",
            "signals": {"error": f"{type(exc).__name__}: {exc}"[:200]},
        }
    return music_signals(samples, sr)


#: Engine fallback name (per-aspect resolution in engine._stage_dressing).
analyse_music = estimate_music_presence


# ---------------------------------------------------------------------------
# Frame sampling (FFmpeg) — shared by vision + heuristic paths
# ---------------------------------------------------------------------------
def _extract_frame_bytes(
    video_path: str, at_seconds: float, *, ffmpeg: str = ffmpeg_executable()
) -> bytes | None:
    """One downscaled JPEG frame at ``at_seconds``, or None on failure."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        out = tf.name
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0.0, at_seconds):.3f}",
                "-i", str(video_path),
                "-frames:v", "1",
                "-vf", f"scale={_FRAME_WIDTH}:-2",
                "-q:v", "4", "-y", out,
            ],
            capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            return None
        data = Path(out).read_bytes()
        return data or None
    except Exception:
        return None
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def sample_frame_bytes(
    video_path: str | os.PathLike,
    duration_s: float,
    *,
    count: int = DEFAULT_FRAME_COUNT,
    ffmpeg: str = ffmpeg_executable(),
) -> tuple[list[bytes], list[float]]:
    """``count`` frames spread evenly over the video + their timestamps.

    Frames sit at bin centres ((i + 0.5) * duration / count) so the
    first/last moments (often black fades / end cards) are not
    oversampled. Failed extractions are skipped, so the returned list
    may be shorter than ``count`` (timestamps stay aligned).
    """
    frames: list[bytes] = []
    stamps: list[float] = []
    if duration_s <= 0:
        return frames, stamps
    for i in range(count):
        t = (i + 0.5) * duration_s / count
        data = _extract_frame_bytes(str(video_path), t, ffmpeg=ffmpeg)
        if data:
            frames.append(data)
            stamps.append(t)
    return frames, stamps


# ---------------------------------------------------------------------------
# 2+3. Caption burn-in & overlays — vision (one batched call)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FrameVerdict:
    """One sampled frame's caption / overlay answers."""

    index: int
    caption: str  # "yes" | "no" | "unsure"
    overlay: str  # "yes" | "no" | "unsure"


_ANSWERS = ("yes", "no", "unsure")

_DRESSING_VISION_PROMPT = """You are checking sampled frames from ONE video for editing "dressing".

For EACH frame, in order, answer exactly two things:
1. "caption": does the frame contain burned-in subtitle/caption text — a line of dialogue text rendered into the picture, usually at the bottom? Answer "yes", "no" or "unsure".
2. "overlay": does the frame contain any OTHER prominent editor-added text overlay that is NOT a bottom subtitle — e.g. a title, a big keyword, a label, a callout or a chapter heading anywhere in the frame? Answer "yes", "no" or "unsure".

Rules:
- Text that is part of the filmed scene itself (signs, book covers, product labels, screens) does NOT count as an overlay unless it clearly looks like a graphic added in editing.
- A video-platform watermark or channel logo alone does NOT count as an overlay.
- If the text is too small or blurry to judge, answer "unsure" — do not guess.

Respond with ONLY a JSON array, one object per frame in the order given, no markdown:
[{"frame": 0, "caption": "yes|no|unsure", "overlay": "yes|no|unsure"}, ...]
"""


def _normalise_verdicts(raw: Any, expected: int) -> list[FrameVerdict] | None:
    """Normalise a vision result (mapping or list) to FrameVerdicts.

    Returns None when nothing usable came back (= vision unavailable,
    caller falls back to the heuristic). Items with unusable answers
    default that answer to "unsure" rather than discarding the frame.
    """
    if raw is None:
        return None
    items: Any = raw.get("frames") if isinstance(raw, Mapping) else raw
    if not isinstance(items, list):
        return None
    verdicts: list[FrameVerdict] = []
    for pos, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        idx = item.get("frame", item.get("index", pos))
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = pos

        def ans(key: str) -> str:
            v = str(item.get(key, "unsure")).strip().lower()
            return v if v in _ANSWERS else "unsure"

        verdicts.append(
            FrameVerdict(index=idx, caption=ans("caption"), overlay=ans("overlay"))
        )
    if not verdicts:
        return None
    verdicts.sort(key=lambda v: v.index)
    return verdicts[:expected] if expected else verdicts


async def _gemini_dressing_judge(
    frames: list[bytes],
    api_key: str,
    *,
    transport: Any = None,
    timeout_s: float = 120.0,
) -> list[FrameVerdict] | None:
    """ONE batched Gemini-vision call for all frames; None on failure.

    Never raises: any HTTP error (incl. 429), timeout or unparseable
    body returns None and the caller degrades to the heuristic path.
    """
    if not frames or not api_key:
        return None
    parts: list[dict[str, Any]] = [{"text": _DRESSING_VISION_PROMPT}]
    for i, data in enumerate(frames):
        parts.append({"text": f"Frame {i}:"})
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
            "temperature": 0.0,
            "maxOutputTokens": 2048,
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
        return _normalise_verdicts(parsed, expected=len(frames))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 2+3 (fallback). Caption / overlay heuristic — lower third band analysis
# ---------------------------------------------------------------------------
def frame_dressing_signals(image_bytes: bytes) -> dict[str, float]:
    """Cheap per-frame signals for the heuristic fallback (PIL+numpy).

    * ``band_edge_density`` / ``band_std``: edge density and contrast
      in the lower third. Burned-in captions are high-contrast glyphs
      on a (usually) darker, flatter strip — they push both up.
    * ``upper_cluster_ratio``: how concentrated upper-region edges are
      in their busiest grid cell vs the median cell. A title / big
      keyword creates one dense cluster; a busy scene spreads edges.
    * ``upper_edge_density``: overall upper-region edge density.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("L")
    gray = np.asarray(img, dtype=np.float64) / 255.0
    h, w = gray.shape
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:] = np.abs(np.diff(gray, axis=1))
    gy[1:, :] = np.abs(np.diff(gray, axis=0))
    # 0.18 (not higher): burned-in text survives an encode/extract
    # round-trip visibly softened — measured band edge density of a
    # known caption fixture halves after x264+JPEG, while its band
    # contrast (band_std) stays high; the std gate does the
    # noise rejection, this threshold just needs to see the glyphs.
    edge = (gx + gy) > 0.18

    band_top = int(h * 0.70)
    band = gray[band_top:, :]
    band_edge = edge[band_top:, :]
    upper = gray[:band_top, :]
    upper_edge = edge[:band_top, :]

    # 3x6 grid over the upper region for the cluster ratio.
    cells: list[float] = []
    rows, cols = 3, 6
    for r in range(rows):
        for c in range(cols):
            cell = upper_edge[
                r * band_top // rows : (r + 1) * band_top // rows,
                c * w // cols : (c + 1) * w // cols,
            ]
            if cell.size:
                cells.append(float(np.mean(cell)))
    median_cell = float(np.median(cells)) if cells else 0.0
    max_cell = float(np.max(cells)) if cells else 0.0
    return {
        "band_edge_density": round(float(np.mean(band_edge)), 4),
        "band_std": round(float(np.std(band)), 4),
        "upper_edge_density": round(float(np.mean(upper_edge)), 4),
        "upper_cluster_ratio": round(max_cell / (median_cell + 0.01), 3),
    }


# Thresholds calibrated on synthetic fixtures (white glyph-bars on a
# smooth gradient vs plain gradients / noise); see test_dressing.py.
_BAND_EDGE_MIN = 0.020
_BAND_STD_MIN = 0.14
_CLUSTER_RATIO_MIN = 2.4
_UPPER_EDGE_MIN = 0.010


def heuristic_frame_verdicts(frames: list[bytes]) -> list[FrameVerdict]:
    """Per-frame yes/no verdicts from the cheap heuristic signals.

    The heuristic can only flag text-*shaped* contrast structure; it
    never answers "unsure" (it has no notion of readability) — its
    weakness is instead expressed by the aggregator's confidence cap
    under ``provenance="heuristic_only"``.
    """
    verdicts: list[FrameVerdict] = []
    for i, data in enumerate(frames):
        try:
            sig = frame_dressing_signals(data)
        except Exception:
            verdicts.append(FrameVerdict(i, "unsure", "unsure"))
            continue
        caption = (
            "yes"
            if (
                sig["band_edge_density"] >= _BAND_EDGE_MIN
                and sig["band_std"] >= _BAND_STD_MIN
            )
            else "no"
        )
        overlay = (
            "yes"
            if (
                sig["upper_cluster_ratio"] >= _CLUSTER_RATIO_MIN
                and sig["upper_edge_density"] >= _UPPER_EDGE_MIN
            )
            else "no"
        )
        verdicts.append(FrameVerdict(i, caption, overlay))
    return verdicts


# ---------------------------------------------------------------------------
# Aggregation (pure — the documented decision rules live here)
# ---------------------------------------------------------------------------
def aggregate_caption_verdicts(
    verdicts: Sequence[FrameVerdict], *, provenance: str
) -> dict[str, Any]:
    """Caption decision rule over per-frame verdicts.

    ``label`` is ``likely`` when the yes-fraction >=
    :data:`CAPTION_YES_FRACTION_LIKELY` (0.50), ``possible`` when >=
    :data:`CAPTION_YES_FRACTION_POSSIBLE` (0.25), else ``unlikely``.
    "unsure" answers count as not-yes and drag confidence down.
    Heuristic provenance caps confidence at 0.45.
    """
    n = len(verdicts)
    if n == 0:
        return {
            "label": "unlikely", "confidence": 0.0,
            "yes_fraction": 0.0, "unsure_fraction": 0.0, "frames": 0,
            "provenance": provenance,
        }
    yes = sum(1 for v in verdicts if v.caption == "yes")
    unsure = sum(1 for v in verdicts if v.caption == "unsure")
    yes_fraction = yes / n
    unsure_fraction = unsure / n
    if yes_fraction >= CAPTION_YES_FRACTION_LIKELY:
        label = "likely"
        confidence = 0.60 + 0.35 * (yes_fraction - 0.5) / 0.5
    elif yes_fraction >= CAPTION_YES_FRACTION_POSSIBLE:
        label = "possible"
        confidence = 0.45
    else:
        label = "unlikely"
        confidence = 0.60 + 0.35 * (0.25 - yes_fraction) / 0.25
    confidence -= 0.30 * unsure_fraction
    if provenance == "heuristic_only":
        confidence = min(confidence, 0.45)
    confidence = float(np.clip(confidence, 0.05, 0.95))
    return {
        "label": label,
        "confidence": round(confidence, 3),
        "yes_fraction": round(yes_fraction, 3),
        "unsure_fraction": round(unsure_fraction, 3),
        "frames": n,
        "provenance": provenance,
    }


def aggregate_overlay_verdicts(
    verdicts: Sequence[FrameVerdict],
    *,
    duration_s: float,
    provenance: str,
) -> dict[str, Any]:
    """Overlay frequency from per-frame verdicts.

    Events = runs of consecutive overlay-"yes" frames in sampled
    order; ``overlays_per_minute`` = events / duration in minutes.
    With ~16 frames over a whole video this *undercounts* short
    overlays — the estimate is a lower bound, and confidence (capped
    0.65 vision / 0.40 heuristic) reflects the sparse sampling.
    """
    n = len(verdicts)
    if n == 0 or duration_s <= 0:
        return {
            "overlays_per_minute": None, "confidence": 0.0,
            "events_observed": 0, "overlay_frame_fraction": 0.0,
            "frames": n, "provenance": provenance,
        }
    positives = [v.overlay == "yes" for v in verdicts]
    unsure_fraction = sum(1 for v in verdicts if v.overlay == "unsure") / n
    events = sum(
        1 for i, p in enumerate(positives) if p and (i == 0 or not positives[i - 1])
    )
    frame_fraction = sum(positives) / n
    per_minute = events / (duration_s / 60.0)
    confidence = 0.35 + 0.25 * min(1.0, n / 16.0) - 0.20 * unsure_fraction
    if provenance == "heuristic_only":
        confidence = min(confidence, 0.40)
    confidence = float(np.clip(confidence, 0.10, 0.65))
    return {
        "overlays_per_minute": round(per_minute, 2),
        "confidence": round(confidence, 3),
        "events_observed": events,
        "overlay_frame_fraction": round(frame_fraction, 3),
        "frames": n,
        "provenance": provenance,
    }


# ---------------------------------------------------------------------------
# Schema mapping
# ---------------------------------------------------------------------------
_MUSIC_TO_PRESENCE = {
    "music_likely": "present",
    "music_possible": "uncertain",
    "no_music_detected": "absent",
}
_CAPTION_TO_PRESENCE = {
    "likely": "present",
    "possible": "uncertain",
    "unlikely": "absent",
}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _analyse_async(
    video_path: str,
    *,
    duration_s: float,
    api_key: str | None,
    vision_fn: VisionFn | None,
    transport: Any,
    frame_count: int,
    ffmpeg: str,
) -> DressingAnalysis:
    notes: list[str] = []

    # -- music (sync CPU work; decode of a few minutes is fast) -------
    music = estimate_music_presence(video_path, ffmpeg=ffmpeg)
    music_label = str(music["label"])
    music_conf = float(music["confidence"])
    if music_conf <= 0.0:
        music_presence = PresenceAssessment(
            label="uncertain", confidence=0.0,
            notes=f"music not assessed: {music['signals'].get('error', 'decode failed')}",
        )
    else:
        sig = music["signals"]
        detail = (
            f"estimator={music_label} score={music['music_score']} "
            f"method={music['method']}"
        )
        if music["method"] == "pause_analysis":
            detail += (
                f" pause_fill_db={sig.get('pause_fill_db')} "
                f"pause_peak_ratio={sig.get('pause_peak_ratio')} "
                f"pauses={sig.get('pause_count')}"
            )
        music_presence = PresenceAssessment(
            label=_MUSIC_TO_PRESENCE.get(music_label, "uncertain"),
            confidence=music_conf,
            notes=detail,
        )
    notes.append(
        "music: pause-fill + pause-tonality estimate (FFmpeg/numpy); "
        "weak indirect signal — a quiet/ducked bed can be missed and "
        "steady noise can imitate one."
    )

    # -- frames --------------------------------------------------------
    frames, _stamps = sample_frame_bytes(
        video_path, duration_s, count=frame_count, ffmpeg=ffmpeg
    )
    if not frames:
        notes.append("captions/overlays: frame sampling failed; not assessed.")
        return DressingAnalysis(
            music_presence=music_presence,
            caption_burn_in=PresenceAssessment(
                label="uncertain", confidence=0.0,
                notes="frame sampling failed",
            ),
            text_overlay_frequency=None,
            text_overlay_confidence=0.0,
            confidence=round(music_conf, 3),
            notes=notes,
        )

    # -- vision (one batched call) or heuristic fallback ---------------
    verdicts: list[FrameVerdict] | None = None
    provenance = "heuristic_only"
    if vision_fn is not None:
        try:
            raw = await _maybe_await(vision_fn(frames))
            verdicts = _normalise_verdicts(raw, expected=len(frames))
        except Exception:
            verdicts = None
        if verdicts is not None:
            provenance = "vision"
    elif api_key:
        verdicts = await _gemini_dressing_judge(
            frames, api_key, transport=transport
        )
        if verdicts is not None:
            provenance = "vision"

    if verdicts is None:
        verdicts = heuristic_frame_verdicts(frames)
        provenance = "heuristic_only"
        notes.append(
            "captions/overlays: vision unavailable — heuristic_only "
            "fallback (lower-third contrast structure); confidence capped."
        )
    else:
        notes.append(
            f"captions/overlays: one batched {VISION_MODEL} vision call "
            f"over {len(frames)} sampled frames."
        )

    caption = aggregate_caption_verdicts(verdicts, provenance=provenance)
    overlay = aggregate_overlay_verdicts(
        verdicts, duration_s=duration_s, provenance=provenance
    )

    caption_presence = PresenceAssessment(
        label=_CAPTION_TO_PRESENCE.get(str(caption["label"]), "uncertain"),
        confidence=float(caption["confidence"]),
        notes=(
            f"estimator={caption['label']} yes_fraction={caption['yes_fraction']} "
            f"unsure_fraction={caption['unsure_fraction']} "
            f"frames={caption['frames']} provenance={provenance}; "
            f"likely requires yes_fraction >= {CAPTION_YES_FRACTION_LIKELY}"
        ),
    )
    notes.append(
        "overlays: events = runs of overlay-positive sampled frames; "
        "sparse sampling undercounts short overlays (lower-bound estimate)."
    )
    overall = (
        music_conf
        + float(caption["confidence"])
        + float(overlay["confidence"])
    ) / 3.0
    return DressingAnalysis(
        music_presence=music_presence,
        caption_burn_in=caption_presence,
        text_overlay_frequency=overlay["overlays_per_minute"],
        text_overlay_confidence=float(overlay["confidence"]),
        confidence=round(float(overall), 3),
        notes=notes,
    )


def analyse_dressing(
    video_path: str | os.PathLike,
    scenes: Optional[Sequence[Any]] = None,
    speech: Any = None,
    *,
    api_key: str | None = None,
    vision_fn: VisionFn | None = None,
    transport: Any = None,
    frame_count: int = DEFAULT_FRAME_COUNT,
    ffmpeg: str = ffmpeg_executable(),
    ffprobe: str = ffprobe_executable(),
) -> DressingAnalysis:
    """Analyse a video's audio/editing dressing.

    Parameters
    ----------
    video_path: path to the video file (must exist).
    scenes: optional scene sequence (dicts/objects with end times) —
        used only as a duration fallback; dressing itself is computed
        over the whole file. Accepted (and ignored beyond duration)
        ``speech`` for engine call-shape compatibility.
    api_key: Gemini API key for the batched vision check. When
        omitted, read from the ``GEMINI_API_KEY`` environment
        variable. With no key and no ``vision_fn``, captions/overlays
        degrade to the ``heuristic_only`` fallback.
    vision_fn: optional injected vision callable (tests /
        alternative providers); takes precedence over the Gemini path.
    frame_count: sampled frames for the caption/overlay check
        (clamped to 12–20).

    Returns a schema :class:`DressingAnalysis`. ``motion`` is left at
    its uncertain default — motion is the scenes stage's job.
    """
    path = str(video_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"video not found: {path}")
    key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")
    duration = probe_duration_s(path, ffprobe=ffprobe)
    if duration is None:
        duration = _duration_from_scenes(scenes)
    if duration is None or duration <= 0:
        raise DressingError(f"could not determine duration of {path}")
    count = int(min(20, max(12, frame_count)))
    return asyncio.run(
        _analyse_async(
            path,
            duration_s=float(duration),
            api_key=key,
            vision_fn=vision_fn,
            transport=transport,
            frame_count=count,
            ffmpeg=ffmpeg,
        )
    )


#: US-spelling alias (engine resolves analyse_dressing first).
analyze_dressing = analyse_dressing
