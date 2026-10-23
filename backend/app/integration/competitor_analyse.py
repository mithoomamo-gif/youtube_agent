"""Style Lab: the competitor-video ANALYSER (W6 — the analysis seam).

This module is the seam the future analysis team plugs into: it turns
ONE competitor video *pack* — the video file plus whatever sidecar
artefacts the pack carries (title/description text, a captions SRT, a
licence-ledger CSV, a thumbnail image) — into one
``competitor_analysis.v1`` dict, the shared contract defined by
:mod:`app.integration.style_profile` (:class:`CompetitorVideoAnalysis`).

:func:`analyse_pack` returns a plain ``dict``.  Its contract keys are
exactly the ``CompetitorVideoAnalysis`` fields; :func:`to_contract_dict`
strips the one extra key, ``analysis_notes``, and
:func:`build_analysis_model` validates the result into the pydantic
model for :func:`app.integration.style_profile.synthesize_style_profile`.

HONESTY RULES (the whole point of this module)
---------------------------------------------
* Every heuristic carries its method + confidence in
  ``analysis_notes`` — nothing here pretends to be more certain than
  it is.
* Shot visual classes come from the pack's LICENCE LEDGER + scene
  timing manifest (timing overlap), NEVER from pixel classification:
  this module does **not** claim to detect "AI vs stock" from pixels.
  A shot that overlaps no ledgered scene is ``"unknown"`` at low
  confidence.
* ``thumbnail.face_present`` is reported ``False`` with the method
  note *"not detectable with current tooling"*: the repo was checked
  (``app/pipeline/thumbnail``, ``app/pipeline/qc``, provider code) and
  contains no face detector, and the venv has no cv2 / mediapipe /
  face-recognition library.  ``False`` here means "not detected by any
  available tool", i.e. effectively unknown — the note says so.
* ``thumbnail.text_word_count`` is ``None``: there is no OCR tooling
  in the repo/venv, so thumbnail text is not counted rather than
  guessed.
* Chapters without timestamps in the description are an EQUAL-SPLIT
  ESTIMATE over the chapter count found there, flagged as such.

Methods (deterministic wherever possible)
------------------------------------------
* duration — ``ffprobe`` ``format=duration`` (high confidence).
* shots — ``ffmpeg`` scene detection,
  ``select='gt(scene,0.4)',showinfo``; shot boundaries are the
  reported ``pts_time`` values (high confidence for hard cuts;
  dissolves / visually-similar cuts can be missed — the notes record
  the cut count so a reader can judge).
* visual_class — timing overlap between each detected shot and the
  timed scenes of the pack's scene manifest (``scene_manifest.json``
  / ``visuals.json``, discovered near the pack files), each scene's
  class read from its licence-ledger row (stock / ai / plate by
  source) or, failing a row, from the scene's own provider/source
  fields.  Ledger-backed overlap -> the scene's class at confidence
  0.9; no overlap -> ``"unknown"`` at 0.2.
* speech WPM — SRT cue-text word count / duration; language from the
  pack's ``publish_manifest.json`` when readable, else ``"ur"``.
* chapters — timestamp lines in the description when present, else
  the description's chapter-line count with equal-split timings
  (estimate, flagged), else a single full-duration span.
* hook — the first chapter's window; its ``type`` is a low-confidence
  label heuristic (question mark in the opening text -> ``"question"``,
  otherwise ``"cold_open"``).
* captions — ``present`` from a readable SRT with cue text;
  ``burned_in`` reported ``False`` at low confidence (detecting
  burned-in captions needs OCR over sampled frames; no OCR tooling
  exists in the repo).
* music — ``present`` from a ``kind == "music"`` licence-ledger row
  when a ledger was supplied (high confidence); without a ledger it
  is reported ``False`` at low confidence (the audio stream alone
  cannot separate a music bed from narration here).
* thumbnail palette — Pillow dominant colour of the thumbnail,
  mapped through HSV into a small fixed palette set (deterministic).

Only stdlib + Pillow are used; ffmpeg/ffprobe are external binaries
already required by the rest of the pipeline.
"""

from __future__ import annotations

import colorsys
import csv
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable
from app.ffmpeg_resolution import ffmpeg_executable, ffprobe_executable

__all__ = [
    "analyse_pack",
    "build_analysis_model",
    "summarise_analysis",
    "to_contract_dict",
]

#: Extra (non-contract) key carrying every method/confidence note.
NOTES_KEY = "analysis_notes"

_SCENE_THRESHOLD = 0.4
_FFPROBE_TIMEOUT_S = 120
_FFMPEG_TIMEOUT_S = 3600

# ---------------------------------------------------------------------------
# Small file / process helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_duration_s(video_path: Path) -> float:
    """Duration via ffprobe (the pipeline's own measurement tool)."""
    proc = subprocess.run(
        [
            ffprobe_executable(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(video_path),
        ],
        capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {video_path}: {proc.stderr.strip()[:400]}")
    payload = json.loads(proc.stdout or "{}")
    duration = float(payload["format"]["duration"])
    if duration <= 0:
        raise RuntimeError(f"ffprobe reported non-positive duration: {duration}")
    return duration


_SHOWINFO_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def _detect_cut_times(
    video_path: Path,
    duration_s: float,
    *,
    threshold: float = _SCENE_THRESHOLD,
    sample_s: float | None = None,
) -> list[float]:
    """Shot-cut times via ffmpeg scene detection.

    Runs ``select='gt(scene,<threshold>)',showinfo`` and returns the
    sorted, de-duplicated ``pts_time`` values strictly inside the
    video.  When ``sample_s`` is set, only the first ``sample_s``
    seconds are decoded (input-side ``-t``) — used for very long
    competitor videos; the caller flags the partial coverage.
    """
    cmd: list[str] = [ffmpeg_executable(), "-hide_banner", "-nostats", "-an"]
    if sample_s is not None and sample_s > 0:
        cmd += ["-t", f"{float(sample_s):.3f}"]
    cmd += [
        "-i", str(video_path),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg scene detection failed for {video_path}: "
            f"{(proc.stderr or '').strip()[-400:]}")
    limit = duration_s if sample_s is None else min(duration_s, float(sample_s))
    cuts: list[float] = []
    for line in (proc.stderr or "").splitlines():
        if "showinfo" not in line:
            continue
        match = _SHOWINFO_PTS_RE.search(line)
        if not match:
            continue
        t = float(match.group(1))
        if 0.05 < t < limit - 0.05:
            if not cuts or t - cuts[-1] > 0.04:
                cuts.append(t)
    return cuts


# ---------------------------------------------------------------------------
# Captions (SRT) + pack manifest
# ---------------------------------------------------------------------------

_SRT_TIME_RE = re.compile(r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->")


def _srt_text(srt_path: Path) -> str:
    """Cue text of an SRT file (indexes + timing lines removed)."""
    raw = srt_path.read_text(encoding="utf-8", errors="replace")
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.isdigit() or _SRT_TIME_RE.search(stripped):
            continue
        lines.append(stripped)
    return " ".join(lines)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


_MANIFEST_NAMES = (
    "publish_manifest.json", "manifest.json", "package.json", "bundle.json",
)
_LANGUAGE_KEYS = (
    "language", "default_audio_language", "caption_language",
    "default_language", "audio_language",
)


def _manifest_language(search_dirs: Iterable[Path]) -> tuple[str | None, str]:
    """(language, method-note) from the first readable pack manifest."""
    seen: set[Path] = set()
    for directory in search_dirs:
        if directory is None or directory in seen:
            continue
        seen.add(directory)
        for name in _MANIFEST_NAMES:
            data = _read_json_file(directory / name)
            if not data:
                continue
            for key in _LANGUAGE_KEYS:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip(), (
                        f"pack manifest {name} field {key!r}")
    return None, "no readable pack manifest language field"


# ---------------------------------------------------------------------------
# Chapters + hook (from the description text)
# ---------------------------------------------------------------------------

_TS_TOKEN = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
_TIMESTAMP_LINE_RE = re.compile(
    rf"^\s*(?:[-*•]\s*)?(?:chapter\s+\d+\s*[:.\-—]?\s*)?({_TS_TOKEN})\b",
    re.IGNORECASE,
)
_CHAPTER_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?chapter\s+\d+\s*[:.\-—]", re.IGNORECASE)
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s+\S")


def _parse_timestamp(token: str) -> float:
    parts = [float(p) for p in token.split(":")]
    total = 0.0
    for part in parts:
        total = total * 60.0 + part
    return total


def _chapter_lines(description: str) -> list[str]:
    return [
        line.strip() for line in (description or "").splitlines()
        if _CHAPTER_LINE_RE.match(line) or _HEADING_LINE_RE.match(line)
    ]


def _chapters_from_description(
    description: str, duration_s: float,
) -> tuple[list[dict[str, float]], dict[str, Any], str | None]:
    """(chapters, note, first_chapter_label).

    Preference order: real timestamp lines in the description;
    chapter-line/heading COUNT with equal-split timings (an estimate,
    flagged); a single full-duration span when no structure exists.
    """
    lines = (description or "").splitlines()
    starts: list[float] = []
    for line in lines:
        match = _TIMESTAMP_LINE_RE.match(line)
        if match:
            t = _parse_timestamp(match.group(1))
            if 0.0 <= t < duration_s and (not starts or t > starts[-1]):
                starts.append(t)
    labels = _chapter_lines(description)
    first_label = labels[0] if labels else None
    if len(starts) >= 2:
        if starts[0] > 0.0:
            starts = [0.0] + starts
        chapters = [
            {
                "start_s": round(start, 3),
                "end_s": round(
                    starts[i + 1] if i + 1 < len(starts) else duration_s, 3),
            }
            for i, start in enumerate(starts)
        ]
        return chapters, {
            "method": "timestamp lines parsed from the description",
            "estimated": False, "confidence": "medium",
            "chapter_count": len(chapters),
        }, first_label
    count = len(labels)
    if count >= 2:
        step = duration_s / count
        chapters = [
            {
                "start_s": round(i * step, 3),
                "end_s": round((i + 1) * step if i + 1 < count else duration_s, 3),
            }
            for i in range(count)
        ]
        return chapters, {
            "method": (
                f"description lists {count} chapter lines without "
                "timestamps; timings are an EQUAL-SPLIT ESTIMATE "
                "(duration / chapter count), not measured boundaries"),
            "estimated": True, "confidence": "low",
            "chapter_count": count,
        }, first_label
    return (
        [{"start_s": 0.0, "end_s": round(duration_s, 3)}],
        {
            "method": (
                "no chapter timestamps or chapter lines found in the "
                "description; a single full-duration span is reported "
                "(placeholder, not a measured chapter structure)"),
            "estimated": True, "confidence": "low", "chapter_count": 1,
        },
        first_label,
    )


def _hook_type(first_label: str | None, opening_text: str) -> tuple[str, str]:
    """Low-confidence hook label from the opening text only."""
    probe = f"{first_label or ''} {opening_text[:400]}"
    if "?" in probe or "？" in probe:
        return "question", (
            "question mark in the first chapter label / opening "
            "caption text -> 'question' (heuristic)")
    return "cold_open", (
        "no question marker in the opening text; defaulted to "
        "'cold_open' (heuristic fallback, not a content judgement)")


# ---------------------------------------------------------------------------
# Licence ledger + scene timing manifest -> timed visual classes
# ---------------------------------------------------------------------------

_STOCK_MARKERS = ("pixabay", "pexels", "unsplash", "wikimedia", "stock")
_AI_MARKERS = ("flux", "stable diffusion", "dall-e", "dalle", "midjourney",
               "ai-generated", "ai generated", "ai image", "ai_image")
_PLATE_MARKERS = ("plate", "pillow", "local plate", "generated plate")


def _classify_source_text(*texts: str | None) -> str | None:
    """Visual class implied by a ledger row / scene source string."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None
    if any(marker in blob for marker in _STOCK_MARKERS):
        return "stock"
    if any(marker in blob for marker in _PLATE_MARKERS):
        return "plate"
    if any(marker in blob for marker in _AI_MARKERS):
        return "ai"
    if "own/generated" in blob or "generated in-app" in blob:
        return "plate"
    return None


def _read_ledger(
    ledger_csv: Path | None,
) -> tuple[dict[str, str], dict[str, str], bool, int]:
    """Parse the licence ledger.

    Returns ``(class_by_asset_id, class_by_scene_id, music_present,
    stock_row_count)``.  Only rows whose source identifies a visual
    class are mapped; voice rows never classify a shot.
    """
    by_asset: dict[str, str] = {}
    by_scene: dict[str, str] = {}
    music_present = False
    stock_rows = 0
    if ledger_csv is None or not Path(ledger_csv).is_file():
        return by_asset, by_scene, music_present, stock_rows
    with Path(ledger_csv).open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            kind = str(row.get("kind") or "").strip().lower()
            if kind == "music":
                music_present = True
                continue
            if kind not in ("image", "video", "visual", ""):
                continue
            cls = _classify_source_text(
                row.get("source"), row.get("licence"), row.get("notes"))
            if cls is None:
                continue
            if cls == "stock":
                stock_rows += 1
            asset_id = str(row.get("asset_id") or "").strip()
            scene_id = str(row.get("scene_id") or "").strip()
            if asset_id:
                by_asset[asset_id] = cls
            if scene_id:
                by_scene[scene_id] = cls
    return by_asset, by_scene, music_present, stock_rows


def _extract_scenes(payload: Any) -> list[dict[str, Any]]:
    """Scene dicts from a scene-manifest payload of any known shape."""
    if isinstance(payload, list):
        scenes = payload
    elif isinstance(payload, dict):
        scenes = (
            payload.get("scenes") or payload.get("scene_manifest")
            or payload.get("items") or [])
    else:
        scenes = []
    out: list[dict[str, Any]] = []
    for scene in scenes if isinstance(scenes, list) else []:
        if not isinstance(scene, dict):
            continue
        try:
            start = float(scene.get("start_s"))
            end = float(scene.get("end_s"))
        except (TypeError, ValueError):
            continue
        if end > start:
            out.append(scene)
    return out


def _find_scene_timings(
    roots: list[Path],
    by_asset: dict[str, str],
    by_scene: dict[str, str],
) -> tuple[list[dict[str, Any]], str | None]:
    """Discover the pack's timed scenes (scene_manifest / visuals JSON).

    Searches each root and one parent level, up to depth 3, and picks
    the candidate whose scenes best match the ledger (asset/scene id
    overlap), then the one with the most scenes.  Trust rule: when
    the ledger yielded ANY id mapping, a candidate with zero overlap
    is never used — it would be some other pack's timings.  Returns
    ``(scenes, path)`` — ``([], None)`` when nothing usable exists.
    """
    search_roots: list[Path] = []
    for root in roots:
        for candidate_root in (root, root.parent):
            if candidate_root not in search_roots:
                search_roots.append(candidate_root)
    best: tuple[int, int, list[dict[str, Any]], str] | None = None
    for root in search_roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth > 3:
                dirnames[:] = []
                continue
            for name in sorted(filenames):
                if name not in ("scene_manifest.json", "visuals.json"):
                    continue
                path = Path(dirpath) / name
                payload = _read_json_file(path)
                if payload is None:
                    # visuals.json may be a bare list in some packs.
                    try:
                        payload = json.loads(
                            path.read_text(encoding="utf-8", errors="replace"))
                    except Exception:
                        continue
                scenes = _extract_scenes(payload)
                if not scenes:
                    continue
                overlap = sum(
                    1 for s in scenes
                    if str(s.get("asset_id") or "") in by_asset
                    or str(s.get("scene_id") or "") in by_scene)
                score = (overlap, len(scenes))
                if best is None or score > (best[0], best[1]):
                    best = (overlap, len(scenes), scenes, str(path))
    if best is None:
        return [], None
    if (by_asset or by_scene) and best[0] == 0:
        return [], None
    return best[2], best[3]


def _timed_class_regions(
    scenes: list[dict[str, Any]],
    by_asset: dict[str, str],
    by_scene: dict[str, str],
) -> list[tuple[float, float, str, str]]:
    """(start, end, visual_class, origin) per timed scene.

    A scene's class comes from its licence-ledger row when one maps
    (origin ``"ledger"``); otherwise from the scene's own
    provider/source fields (origin ``"scene_manifest"``).  Scenes
    classifiable neither way are omitted — shots overlapping only
    them stay ``"unknown"``.
    """
    regions: list[tuple[float, float, str, str]] = []
    for scene in scenes:
        cls = (
            by_asset.get(str(scene.get("asset_id") or ""))
            or by_scene.get(str(scene.get("scene_id") or "")))
        origin = "ledger"
        if cls is None:
            cls = _classify_source_text(
                scene.get("provider"), scene.get("source"))
            origin = "scene_manifest"
        if cls is None:
            continue
        regions.append(
            (float(scene["start_s"]), float(scene["end_s"]), cls, origin))
    regions.sort(key=lambda r: r[0])
    return regions


def _classify_shot(
    start: float, end: float,
    regions: list[tuple[float, float, str, str]],
) -> tuple[str, float, str]:
    """(visual_class, confidence, origin) for one detected shot.

    Overlap is aggregated PER CLASS across all timed regions (a shot
    spanning three stock scenes is fully stock-covered, not 1/3).
    The winning class is the one covering the most of the shot.
    """
    per_class: dict[str, float] = {}
    per_class_origin: dict[str, str] = {}
    for r_start, r_end, cls, origin in regions:
        overlap = min(end, r_end) - max(start, r_start)
        if overlap <= 0:
            continue
        per_class[cls] = per_class.get(cls, 0.0) + overlap
        if origin == "ledger" or cls not in per_class_origin:
            per_class_origin[cls] = origin
    if not per_class:
        return "unknown", 0.2, "none"
    best_cls = max(per_class, key=lambda c: per_class[c])
    best_origin = per_class_origin[best_cls]
    # Ledger-backed timing overlap is the prescribed high-confidence
    # path; a class read only from scene-manifest fields is a notch
    # lower; a sliver of coverage (<25% of the shot) lower still.
    fraction = per_class[best_cls] / max(end - start, 1e-9)
    if fraction < 0.25:
        return best_cls, 0.5, best_origin
    return best_cls, 0.9 if best_origin == "ledger" else 0.8, best_origin


# ---------------------------------------------------------------------------
# Thumbnail heuristics (Pillow)
# ---------------------------------------------------------------------------

#: The small fixed palette set dominant colours are mapped into.
PALETTE_CLASSES = (
    "deep_blue", "bright_blue", "teal", "green", "warm_orange",
    "warm_yellow", "red", "purple", "magenta",
    "dark_neutral", "light_neutral", "neutral",
)


def _palette_class(rgb: tuple[int, int, int]) -> str:
    """Map one RGB colour into :data:`PALETTE_CLASSES` via HSV."""
    h, s, v = colorsys.rgb_to_hsv(
        rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    if s < 0.15:
        if v < 0.25:
            return "dark_neutral"
        if v > 0.8:
            return "light_neutral"
        return "neutral"
    hue = h * 360.0
    if hue < 15 or hue >= 345:
        return "red"
    if hue < 45:
        return "warm_orange"
    if hue < 70:
        return "warm_yellow"
    if hue < 160:
        return "green"
    if hue < 195:
        return "teal"
    if hue < 255:
        return "deep_blue" if v < 0.5 else "bright_blue"
    if hue < 290:
        return "purple"
    return "magenta"


def _analyse_thumbnail(
    thumbnail_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(thumbnail contract dict, note) — Pillow heuristics only."""
    note: dict[str, Any] = {
        "face_present": (
            "not detectable with current tooling: no face detector "
            "exists in the repo (checked app/pipeline/thumbnail and "
            "app/pipeline/qc; the venv has no cv2/mediapipe/face "
            "library), so face_present is reported False meaning "
            "'not detected' — effectively unknown"),
        "face_confidence": "low",
        "text_word_count": (
            "not counted: no OCR tooling exists in the repo/venv "
            "(no pytesseract/easyocr), so the count is left null "
            "rather than guessed"),
        "layout_class": (
            "left null: layout cannot be classified reliably from "
            "pixels without a vision/OCR model; null means 'no "
            "opinion' to downstream synthesis"),
    }
    thumbnail: dict[str, Any] = {
        "layout_class": None,
        "text_word_count": None,
        "face_present": False,
        "palette_class": None,
    }
    if thumbnail_path is None or not Path(thumbnail_path).is_file():
        note["palette_class"] = "no thumbnail supplied/found"
        return thumbnail, note
    try:
        from PIL import Image

        with Image.open(thumbnail_path) as image:
            rgb = image.convert("RGB")
            small = rgb.resize((64, 36))
        counts: dict[tuple[int, int, int], int] = {}
        raw = small.tobytes()
        for i in range(0, len(raw) - 2, 3):
            bucket = tuple(
                int(c // 32) * 32 + 16 for c in raw[i:i + 3])
            counts[bucket] = counts.get(bucket, 0) + 1  # type: ignore[arg-type]
        dominant = max(counts, key=lambda k: counts[k])
        thumbnail["palette_class"] = _palette_class(dominant)  # type: ignore[arg-type]
        note["palette_class"] = {
            "method": (
                "Pillow: thumbnail downscaled to 64x36, pixels "
                "bucketed to 32-step RGB, dominant bucket centre "
                "mapped through HSV into the fixed palette set"),
            "dominant_rgb": list(dominant),
            "confidence": "medium",
        }
    except Exception as exc:  # unreadable image: stay honest, stay null
        note["palette_class"] = f"thumbnail unreadable ({type(exc).__name__})"
    return thumbnail, note


# ---------------------------------------------------------------------------
# The analyser
# ---------------------------------------------------------------------------


def analyse_pack(
    video_path: str | Path,
    *,
    title: str,
    description: str,
    captions_srt: str | Path | None = None,
    licence_ledger_csv: str | Path | None = None,
    thumbnail_path: str | Path | None = None,
    asset_id: str,
    source_kind: str = "attached_file",
    scene_sample_s: float | None = None,
) -> dict[str, Any]:
    """Measure one competitor video pack -> ``competitor_analysis.v1`` dict.

    ``title`` / ``description`` are the video's public SEO texts: they
    are carried into the analysis (``seo``) for pattern extraction
    ONLY — synthesis into a style profile is where the no-copy rule
    strips them (see :mod:`app.integration.style_profile`).

    The returned dict's contract keys validate as
    :class:`app.integration.style_profile.CompetitorVideoAnalysis`
    once the extra ``analysis_notes`` key is removed (see
    :func:`to_contract_dict` / :func:`build_analysis_model`).
    ``scene_sample_s`` optionally limits scene detection to the first
    N seconds (very long videos); coverage is flagged in the notes
    and shots then span only the sampled window.
    """
    video = Path(video_path)
    if not video.is_file():
        raise FileNotFoundError(f"video file not found: {video}")
    captions = Path(captions_srt) if captions_srt else None
    ledger = Path(licence_ledger_csv) if licence_ledger_csv else None
    thumbnail = Path(thumbnail_path) if thumbnail_path else None

    duration_s = _probe_duration_s(video)
    notes: dict[str, Any] = {
        "duration": {
            "method": "ffprobe format=duration", "confidence": "high"},
        "source_sha256": {
            "method": "sha256 of the video file bytes",
            "confidence": "high"},
    }

    # -- speech -------------------------------------------------------------
    word_count: int | None = None
    wpm: float | None = None
    opening_text = ""
    captions_present = False
    if captions is not None and captions.is_file():
        text = _srt_text(captions)
        if text:
            captions_present = True
            opening_text = text
            word_count = len(text.split())
            wpm = round(word_count / (duration_s / 60.0), 2)
    language, language_method = _manifest_language(
        [video.parent,
         captions.parent if captions else None,  # type: ignore[arg-type]
         ledger.parent if ledger else None])  # type: ignore[arg-type]
    if language is None:
        language = "ur"
        language_method = "default 'ur' (no readable pack manifest)"
    notes["speech"] = {
        "method": (
            "words = whitespace tokens of the SRT cue text; "
            "wpm = word_count / (duration_s / 60)"),
        "confidence": "medium" if captions_present else "low",
        "caveat": (
            "WPM inherits any speech-recognition errors in the SRT; "
            "it measures the caption text, not a re-transcription"),
        "language_method": language_method,
    }

    # -- chapters + hook ------------------------------------------------------
    chapters, chapter_note, first_label = _chapters_from_description(
        description or "", duration_s)
    notes["chapters"] = chapter_note
    hook_window = (
        round(chapters[0]["end_s"] - chapters[0]["start_s"], 3)
        if chapters else None)
    hook_type, hook_type_method = _hook_type(first_label, opening_text)
    notes["hook"] = {
        "method": "window = duration of the first chapter",
        "window_confidence": chapter_note.get("confidence", "low"),
        "type_method": hook_type_method,
        "type_confidence": "low",
    }

    # -- shots + visual classes ----------------------------------------------
    cuts = _detect_cut_times(
        video, duration_s, sample_s=scene_sample_s)
    coverage_end = (
        duration_s if scene_sample_s is None
        else min(duration_s, float(scene_sample_s)))
    boundaries = [0.0] + cuts + [coverage_end]
    raw_shots = [
        (round(boundaries[i], 3), round(boundaries[i + 1], 3))
        for i in range(len(boundaries) - 1)
        if boundaries[i + 1] > boundaries[i]
    ]
    by_asset, by_scene, music_present, stock_rows = _read_ledger(ledger)
    scenes, scenes_path = _find_scene_timings(
        [p for p in (video.parent,
                     ledger.parent if ledger else None,
                     captions.parent if captions else None,
                     thumbnail.parent if thumbnail else None)
         if p is not None],
        by_asset, by_scene)
    regions = _timed_class_regions(scenes, by_asset, by_scene)
    shots: list[dict[str, Any]] = []
    class_time: dict[str, float] = {}
    for start, end in raw_shots:
        cls, confidence, origin = _classify_shot(start, end, regions)
        shots.append({
            "start_s": start, "end_s": end,
            "visual_class": cls, "confidence": confidence,
        })
        class_time[cls] = class_time.get(cls, 0.0) + (end - start)
    total_shot_time = sum(end - start for start, end in raw_shots) or 0.0
    stock_share = (
        round(class_time.get("stock", 0.0) / total_shot_time, 4)
        if total_shot_time else 0.0)
    notes["shots"] = {
        "method": (
            "ffmpeg scene detection select='gt(scene,0.4)' "
            "(showinfo pts_time = cut points)"),
        "confidence": (
            "high for hard cuts; visually similar consecutive scenes "
            "and dissolves can score below 0.4 and be missed"),
        "cut_count": len(cuts),
        "shot_count": len(shots),
        "coverage_end_s": round(coverage_end, 3),
        "sampled": scene_sample_s is not None,
    }
    notes["visual_class"] = {
        "method": (
            "timing overlap with the pack's timed scenes; each "
            "scene's class from its licence-ledger row (source -> "
            "stock/ai/plate), falling back to the scene's own "
            "provider/source fields. NOT detected from pixels: "
            "AI-vs-stock pixel classification is not performed"),
        "scene_manifest": scenes_path,
        "timed_scenes_found": len(scenes),
        "ledger_stock_rows": stock_rows,
        "stock_share_by_time": stock_share,
        "class_time_s": {k: round(v, 2) for k, v in sorted(class_time.items())},
        "confidence": (
            "0.9 per ledger-backed shot, 0.8 per scene-manifest-only "
            "shot, 0.5 for a sliver overlap, 0.2 for 'unknown'"),
    }

    # -- music ------------------------------------------------------------------
    if ledger is not None and ledger.is_file():
        notes["music"] = {
            "method": "licence-ledger row with kind == 'music'",
            "confidence": "high", "ledger": str(ledger),
        }
    else:
        music_present = False
        notes["music"] = {
            "method": (
                "no licence ledger supplied; music cannot be "
                "separated from narration from the audio stream "
                "alone with current tooling, so present is reported "
                "False at low confidence (effectively unknown)"),
            "confidence": "low",
        }

    # -- thumbnail ----------------------------------------------------------------
    thumbnail_data, thumbnail_note = _analyse_thumbnail(thumbnail)
    notes["thumbnail"] = thumbnail_note

    notes["captions"] = {
        "present_method": "sidecar SRT supplied and contains cue text",
        "burned_in": (
            "reported False: detecting burned-in captions requires "
            "OCR over sampled frames and no OCR tooling exists in "
            "the repo; a sidecar SRT means captions exist as a "
            "track. Low confidence — effectively unknown"),
        "burned_in_confidence": "low",
    }

    return {
        "schema_version": "competitor_analysis.v1",
        "source": {
            "kind": source_kind,
            "asset_id": asset_id,
            "sha256": _sha256_file(video),
        },
        "duration_s": round(duration_s, 3),
        "speech": {
            "wpm": wpm, "language": language, "word_count": word_count},
        "hook": {"window_s": hook_window, "type": hook_type},
        "chapters": chapters,
        "shots": shots,
        "captions": {"present": captions_present, "burned_in": False},
        "music": {"present": music_present},
        "thumbnail": thumbnail_data,
        "seo": {
            "title_text": title or None,
            "description_text": description or None,
        },
        NOTES_KEY: notes,
    }


# ---------------------------------------------------------------------------
# Contract helpers
# ---------------------------------------------------------------------------


def to_contract_dict(analysis: dict[str, Any]) -> dict[str, Any]:
    """The analysis dict without ``analysis_notes`` (contract-pure)."""
    return {k: v for k, v in analysis.items() if k != NOTES_KEY}


def build_analysis_model(analysis: dict[str, Any]) -> Any:
    """Validate an :func:`analyse_pack` result as the contract model."""
    from app.integration.style_profile import CompetitorVideoAnalysis

    return CompetitorVideoAnalysis.model_validate(to_contract_dict(analysis))


def summarise_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Derived comparison numbers for one analysis (proof/report use).

    Mirrors the aggregation semantics of
    :func:`app.integration.style_profile.synthesize_style_profile`:
    ratios are fractions of total shot time; the average shot length
    is the median shot length.
    """
    shots = analysis.get("shots") or []
    duration_s = float(analysis.get("duration_s") or 0.0)
    lengths = [
        max(float(s["end_s"]) - float(s["start_s"]), 0.0) for s in shots]
    total = sum(lengths)
    stock_time = sum(
        length for length, s in zip(lengths, shots)
        if s.get("visual_class") == "stock")

    def _median(values: list[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    return {
        "duration_s": duration_s,
        "shot_count": len(shots),
        "avg_shot_len_s": (
            round(_median(lengths), 3) if lengths else None),
        "mean_shot_len_s": (
            round(total / len(lengths), 3) if lengths else None),
        "cuts_per_min": (
            round(len(shots) / (duration_s / 60.0), 3)
            if duration_s else None),
        "stock_share": round(stock_time / total, 4) if total else 0.0,
        "speech_wpm": (analysis.get("speech") or {}).get("wpm"),
        "word_count": (analysis.get("speech") or {}).get("word_count"),
        "chapter_count": len(analysis.get("chapters") or []),
        "hook_window_s": (analysis.get("hook") or {}).get("window_s"),
    }
