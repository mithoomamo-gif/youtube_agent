"""Style Lab — engine orchestration.

``analyse_video()`` runs the full Style Lab pipeline over one video:

    probe metadata -> scene detection -> visual classification ->
    speech (transcribe + structure) -> dressing (music / captions /
    overlays) -> optional thumbnail analysis -> assemble the schema
    result -> persist via storage (JSON + markdown breakdown) ->
    return the result.

Contracts consumed (``app.style_analysis.schema`` is the source of truth for
field names; sibling modules are consumed, never modified):

* ``scenes.analyse_video(video)`` (rich) or ``scenes.detect_scenes``
  -> scene list, converted to ``schema.SceneAnalysis``.
* ``visual_classify.classify_scenes(video, scenes)`` ->
  ``{"scenes": [{scene_index, label, confidence, signals, ...}],
  "visual_mix": {class: pct}, ...}``, converted to ``schema.VisualMix``
  (the classifier's own aggregate percentages are preserved verbatim).
* ``speech.analyse_video_speech(video)`` (or ``analyse_speech`` /
  ``transcribe`` + ``analyse_structure``) -> dict in the schema's
  speech shape (plus extras), converted to ``schema.SpeechAnalysis``.
* ``dressing.analyse_dressing(video, scenes)`` ->
  ``schema.DressingAnalysis`` (model or dict).
* ``thumbnail.analyse_thumbnail(path)`` -> dict in the schema's
  thumbnail shape (plus extras), converted to
  ``schema.ThumbnailAnalysis`` (structure only, per the module's own
  hard boundary).
* ``storage.save_analysis(analysis, base_dir)`` persists
  ``analysis.json`` + ``breakdown.md`` + ``index.json``.

Graceful degradation (the honesty contract)
-------------------------------------------
Every stage is wrapped. A stage whose sibling module is missing /
unimportable / exposes no usable entry point is stubbed with an
explicit provenance entry of status ``"stage_unavailable"``; a stage
that raises at runtime is recorded with status ``"error"`` and the
exception *class name* (never a traceback, never a secret). Either
way the section falls back to the schema's ``make_uncertain()`` state
(thumbnail: ``None``) and the analysis continues — the engine never
crashes a whole analysis because one stage failed.

The returned object is a ``schema.StyleLabAnalysis`` whenever the
schema module is importable (the normal case). Engine bookkeeping
that the schema deliberately does not carry — per-stage
``provenance`` and ``stage_timings`` — is attached to the returned
object as plain attributes (``result.provenance`` /
``result.stage_timings`` / ``result.engine_payload``) and included in
the engine-fallback persistence JSON. If the schema module itself is
unavailable, an :class:`EngineAnalysisResult` wrapper carrying the
same payload dict is returned instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..ffmpeg_resolution import ffprobe_executable

__all__ = [
    "analyse_video",
    "analyze_video",
    "AnalysisResult",
    "EngineAnalysisResult",
    "StageProvenance",
    "compute_visual_mix",
    "result_payload",
    "STATUS_OK",
    "STATUS_STAGE_UNAVAILABLE",
    "STATUS_ERROR",
    "STATUS_SKIPPED",
    "STATUS_UNCERTAIN",
]

# ---------------------------------------------------------------------------
# Stage status vocabulary
# ---------------------------------------------------------------------------

STATUS_OK = "ok"
#: Sibling module missing / unimportable / exposes no usable entry point.
STATUS_STAGE_UNAVAILABLE = "stage_unavailable"
#: Stage ran but raised at runtime (or its output failed conversion).
STATUS_ERROR = "error"
#: Stage intentionally not run (e.g. no thumbnail supplied).
STATUS_SKIPPED = "skipped"
#: Stage ran but reported itself uncertain / partial.
STATUS_UNCERTAIN = "uncertain"

_MAX_ERR_MSG = 300


# ---------------------------------------------------------------------------
# Provenance + fallback result types
# ---------------------------------------------------------------------------


@dataclass
class StageProvenance:
    """One stage's audit entry: what ran, how long, how it ended."""

    stage: str
    status: str
    wall_s: float = 0.0
    error_class: Optional[str] = None
    error_message: Optional[str] = None
    detail: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class EngineAnalysisResult:
    """Engine-owned fallback wrapper (schema module unavailable only).

    ``data`` is the full assembled payload dict; attribute access
    falls through to it.
    """

    data: dict[str, Any] = field(default_factory=dict)

    @property
    def provenance(self) -> list[dict[str, Any]]:
        return self.data.get("provenance", [])

    @property
    def stage_timings(self) -> dict[str, float]:
        return self.data.get("stage_timings", {})

    def to_dict(self) -> dict[str, Any]:
        return self.data

    def __getattr__(self, name: str) -> Any:
        data = self.__dict__.get("data") or {}
        if name in data:
            return data[name]
        raise AttributeError(name)


#: Public alias — the engine's return-type name regardless of backend.
AnalysisResult = EngineAnalysisResult


class _StageUnavailable(Exception):
    """Internal: sibling module/stage entry point is not usable."""

    def __init__(self, stage: str, cause: Optional[BaseException]):
        self.stage = stage
        self.cause = cause
        super().__init__(f"stage {stage} unavailable")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_plain(obj: Any) -> Any:
    """Best-effort conversion to plain JSON-able data."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _to_plain(dataclasses.asdict(obj))
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _to_plain(fn(mode="json") if attr == "model_dump" else fn())
            except TypeError:
                try:
                    return _to_plain(fn())
                except Exception:
                    pass
            except Exception:
                pass
    if hasattr(obj, "__dict__"):
        return {
            str(k): _to_plain(v)
            for k, v in vars(obj).items()
            if not str(k).startswith("_")
        }
    return str(obj)


def _err_text(exc: BaseException) -> str:
    msg = str(exc) or exc.__class__.__name__
    return msg[:_MAX_ERR_MSG]


def _first_callable(module: Any, names: tuple[str, ...]) -> Optional[Callable]:
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value


def _call_flexible(fn: Callable, *candidates: tuple[tuple, dict]) -> Any:
    """Call ``fn`` trying each ``(args, kwargs)`` candidate in order.

    Sibling contracts were built in parallel; signatures differ in
    small ways. We try the contract-documented call first and fall
    back on ``TypeError`` only — genuine runtime errors propagate so
    they are recorded honestly.
    """
    last_type_error: Optional[TypeError] = None
    for args, kwargs in candidates:
        try:
            return _maybe_await(fn(*args, **kwargs))
        except TypeError as exc:
            last_type_error = exc
            continue
    assert last_type_error is not None
    raise last_type_error


def _import_sibling(modname: str):
    """Import ``app.style_analysis.<modname>``; return (module, error)."""
    try:
        import importlib

        mod = importlib.import_module(f"app.style_analysis.{modname}")
        return mod, None
    except Exception as exc:  # ImportError or a broken module body
        return None, exc


def _schema():
    mod, err = _import_sibling("schema")
    if mod is None:
        raise _StageUnavailable("schema", err)
    return mod


# ---------------------------------------------------------------------------
# Stage: probe (engine-owned, ffprobe)
# ---------------------------------------------------------------------------


def probe_video(video_path: Path) -> dict[str, Any]:
    """Probe container/stream metadata with ffprobe (JSON output).

    Falls back to basic file facts when ffprobe is unavailable so the
    probe stage degrades instead of failing the analysis.
    """
    info: dict[str, Any] = {
        "path": str(video_path),
        "exists": video_path.exists(),
    }
    if video_path.exists():
        info["size_bytes"] = video_path.stat().st_size
    try:
        proc = subprocess.run(
            [
                ffprobe_executable(), "-v", "error",
                "-show_format", "-show_streams",
                "-of", "json", str(video_path),
            ],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            info["ffprobe_error"] = (proc.stderr or "")[:_MAX_ERR_MSG]
            return info
        raw = json.loads(proc.stdout or "{}")
        fmt = raw.get("format") or {}
        streams = raw.get("streams") or []
        video_stream = next(
            (s for s in streams if s.get("codec_type") == "video"), {}
        )
        audio_stream = next(
            (s for s in streams if s.get("codec_type") == "audio"), {}
        )

        def _f(value: Any) -> Optional[float]:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        fps = None
        rate = video_stream.get("avg_frame_rate") or ""
        if "/" in str(rate):
            num, _, den = str(rate).partition("/")
            try:
                if float(den) != 0:
                    fps = float(num) / float(den)
            except ValueError:
                fps = None
        info.update(
            {
                "duration_s": _f(fmt.get("duration"))
                or _f(video_stream.get("duration")),
                "format_name": fmt.get("format_name"),
                "bit_rate": _f(fmt.get("bit_rate")),
                "width": video_stream.get("width"),
                "height": video_stream.get("height"),
                "video_codec": video_stream.get("codec_name"),
                "fps": fps,
                "audio_codec": audio_stream.get("codec_name"),
                "audio_channels": audio_stream.get("channels"),
                "has_audio": bool(audio_stream),
                "has_video": bool(video_stream),
            }
        )
    except FileNotFoundError:
        info["ffprobe_error"] = "ffprobe not found on PATH"
    except Exception as exc:  # probe must never kill the analysis
        info["ffprobe_error"] = f"{exc.__class__.__name__}: {_err_text(exc)}"
    return info


# ---------------------------------------------------------------------------
# Schema conversions (raw sibling output -> schema models)
# ---------------------------------------------------------------------------


def _to_schema_scenes(raw: Any, probe_duration: Optional[float]) -> Any:
    """Convert scenes-module output to ``schema.SceneAnalysis``."""
    schema = _schema()
    plain = _to_plain(raw)
    segments: list[Any] = []
    notes: list[str] = []
    if isinstance(plain, dict) and isinstance(plain.get("scenes"), list):
        # Rich scenes.SceneAnalysis / SceneSegmentation shape.
        for entry in plain["scenes"]:
            if isinstance(entry, dict):
                segments.append(entry)
        for note in plain.get("notes") or []:
            notes.append(str(note))
        if plain.get("low_segmentation"):
            notes.append("low_segmentation flagged by scene detector")
    elif isinstance(plain, list):
        segments = [e for e in plain if isinstance(e, dict)]
    segs = []
    for i, entry in enumerate(segments):
        try:
            segs.append(
                schema.SceneSegment(
                    index=int(entry.get("index", entry.get("scene_index", i))),
                    start_s=float(entry.get("start_s", entry.get("start", 0.0))),
                    end_s=float(entry.get("end_s", entry.get("end", 0.0))),
                    duration_s=float(entry.get("duration_s", entry.get("duration", 0.0)) or 0.0),
                )
            )
        except Exception:
            continue  # drop unparseable segment, never fabricate one
    return schema.SceneAnalysis.from_segments(
        segs, total_duration_s=probe_duration, notes=notes
    )


def _to_schema_visual(raw: Any, scene_analysis: Any) -> Any:
    """Convert visual_classify output to ``schema.VisualMix``.

    The classifier's own ``visual_mix`` percentages are preserved
    verbatim; per-scene labels become ``SceneVisualLabel`` rows.
    """
    schema = _schema()
    plain = _to_plain(raw)
    labels: list[Any] = []
    mix_pct: Optional[dict] = None
    notes: list[str] = []
    scene_rows: list[dict] = []
    if isinstance(plain, dict):
        if isinstance(plain.get("scenes"), list):
            scene_rows = [e for e in plain["scenes"] if isinstance(e, dict)]
        elif isinstance(plain.get("scene_labels"), list):
            scene_rows = [e for e in plain["scene_labels"] if isinstance(e, dict)]
        if isinstance(plain.get("visual_mix"), dict):
            mix_pct = plain["visual_mix"]
        for note in plain.get("notes") or []:
            notes.append(str(note))
    elif isinstance(plain, list):
        scene_rows = [e for e in plain if isinstance(e, dict)]
    valid = set(schema.VISUAL_CLASSES)
    for i, row in enumerate(scene_rows):
        label = str(row.get("label", "uncertain"))
        if label not in valid:
            label = "uncertain"
        signals = row.get("signals") or []
        if not isinstance(signals, list):
            signals = [str(signals)]
        candidate = row.get("candidate_label")
        if candidate and label == "uncertain":
            signals = [*signals, f"candidate:{candidate}"]
        try:
            conf = float(row.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        labels.append(
            schema.SceneVisualLabel(
                scene_index=int(row.get("scene_index", row.get("index", i))),
                label=label,
                confidence=max(0.0, min(1.0, conf)),
                signals=[str(s) for s in signals],
            )
        )
    if mix_pct:
        pct = {}
        for name in schema.VISUAL_CLASSES:
            try:
                pct[name] = float(mix_pct.get(name, 0.0) or 0.0)
            except (TypeError, ValueError):
                pct[name] = 0.0
        mean_conf = (
            sum(lbl.confidence for lbl in labels) / len(labels) if labels else 0.0
        )
        try:
            return schema.VisualMix(
                **pct, scene_labels=labels, confidence=round(mean_conf, 4),
                notes=notes,
            )
        except Exception:
            pass  # fall through to derivation from labels
    segments = list(getattr(scene_analysis, "scenes", []) or [])
    mean_conf = (
        sum(lbl.confidence for lbl in labels) / len(labels) if labels else 0.0
    )
    return schema.VisualMix.from_scene_labels(
        labels, segments, confidence=round(mean_conf, 4), notes=notes
    )


def _to_schema_speech(raw: Any) -> Any:
    """Convert speech-module output to ``schema.SpeechAnalysis``."""
    schema = _schema()
    if isinstance(raw, schema.SpeechAnalysis):
        return raw
    plain = _to_plain(raw)
    if not isinstance(plain, dict):
        raise TypeError(f"speech stage returned {type(raw).__name__}, expected mapping")
    hook_in = plain.get("hook") or {}
    hook = schema.HookAnalysis(
        window_s=hook_in.get("window_s"),
        pattern_notes=str(hook_in.get("pattern_notes", "") or ""),
        pattern_label=str(
            hook_in.get("pattern_label", hook_in.get("pattern", "uncertain")) or "uncertain"
        ),
        confidence=float(hook_in.get("confidence", 0.0) or 0.0),
    )
    beats = []
    for b in plain.get("beats") or []:
        if not isinstance(b, dict):
            continue
        beats.append(
            schema.SpeechBeat(
                start_s=float(b.get("start_s", b.get("start", 0.0)) or 0.0),
                end_s=float(b.get("end_s", b.get("end", 0.0)) or 0.0),
                summary=str(b.get("summary", "") or ""),
                confidence=float(b.get("confidence", 0.0) or 0.0),
            )
        )
    cta_in = plain.get("cta") or {}
    placements = []
    for p in cta_in.get("placements") or []:
        if not isinstance(p, dict):
            continue
        notes = ""
        if p.get("position"):
            notes = f"position: {p['position']}"
        if p.get("notes"):
            notes = str(p["notes"])
        placements.append(
            schema.CTAPlacement(
                start_s=p.get("start_s", p.get("timestamp_s")),
                end_s=p.get("end_s"),
                kind=str(p.get("kind", "other") or "other"),
                notes=notes,
                confidence=float(p.get("confidence", 0.0) or 0.0),
            )
        )
    cta = schema.CTAAnalysis(
        present=cta_in.get("present"),
        placements=placements,
        confidence=float(cta_in.get("confidence", 0.0) or 0.0),
    )
    notes: list[str] = []
    if plain.get("language_note"):
        notes.append(str(plain["language_note"]))
    for note in plain.get("notes") or []:
        notes.append(str(note))
    wpm = plain.get("wpm")
    return schema.SpeechAnalysis(
        transcript_languages=[str(x) for x in (plain.get("transcript_languages") or [])],
        language_confidence=float(plain.get("language_confidence", 0.0) or 0.0),
        wpm=float(wpm) if wpm is not None else None,
        wpm_confidence=float(plain.get("wpm_confidence", 0.0) or 0.0),
        hook=hook,
        beats=beats,
        cta=cta,
        transcript_ref=plain.get("transcript_ref"),
        confidence=float(plain.get("confidence", 0.0) or 0.0),
        notes=notes,
    )


def _to_schema_dressing(raw: Any) -> Any:
    """Convert dressing-module output to ``schema.DressingAnalysis``."""
    schema = _schema()
    if isinstance(raw, schema.DressingAnalysis):
        return raw
    plain = _to_plain(raw)
    if not isinstance(plain, dict):
        raise TypeError(f"dressing stage returned {type(raw).__name__}, expected mapping")
    # Direct schema-shaped dict: validate (extras are stripped first
    # because the schema forbids them).
    allowed = set(schema.DressingAnalysis.model_fields)
    candidate = {k: v for k, v in plain.items() if k in allowed}
    if candidate:
        try:
            return schema.DressingAnalysis.model_validate(candidate)
        except Exception:
            pass
    # Aspect-shaped dict ({music: ..., captions: ..., overlays: ...}).
    def _presence(value: Any) -> Any:
        if isinstance(value, dict):
            label = str(value.get("label", "uncertain"))
            if label not in ("present", "absent", "uncertain"):
                label = "uncertain"
            return schema.PresenceAssessment(
                label=label,
                confidence=float(value.get("confidence", 0.0) or 0.0),
                notes=str(value.get("notes", "") or ""),
            )
        if isinstance(value, bool):
            return schema.PresenceAssessment(
                label="present" if value else "absent", confidence=0.0
            )
        return schema.PresenceAssessment()

    overlay_freq = None
    overlays = plain.get("overlays")
    if isinstance(overlays, dict):
        overlay_freq = overlays.get("per_minute", overlays.get("frequency"))
    motion = schema.MotionEstimate()
    motion_in = plain.get("motion")
    if isinstance(motion_in, dict):
        label = str(motion_in.get("label", "uncertain"))
        if label not in ("static", "zoom", "pan", "zoom_pan", "mixed", "uncertain"):
            label = "uncertain"
        motion = schema.MotionEstimate(
            label=label,
            confidence=float(motion_in.get("confidence", 0.0) or 0.0),
            notes=str(motion_in.get("notes", "") or ""),
        )
    return schema.DressingAnalysis(
        music_presence=_presence(plain.get("music", plain.get("music_presence"))),
        caption_burn_in=_presence(plain.get("captions", plain.get("caption_burn_in"))),
        text_overlay_frequency=float(overlay_freq) if overlay_freq is not None else None,
        motion=motion,
        notes=[str(n) for n in (plain.get("notes") or [])],
    )


def _to_schema_thumbnail(raw: Any) -> Optional[Any]:
    """Convert thumbnail-module output to ``schema.ThumbnailAnalysis``."""
    schema = _schema()
    if raw is None:
        return None
    if isinstance(raw, schema.ThumbnailAnalysis):
        return raw
    plain = _to_plain(raw)
    if not isinstance(plain, dict):
        raise TypeError(f"thumbnail stage returned {type(raw).__name__}, expected mapping")
    face = plain.get("face_present")
    if isinstance(face, str):
        face = True if face == "true" else (False if face == "false" else None)
    colours: list[str] = []
    for entry in plain.get("dominant_colours") or []:
        if isinstance(entry, dict) and entry.get("hex"):
            colours.append(str(entry["hex"]))
        elif isinstance(entry, str):
            colours.append(entry)
    notes: list[str] = []
    prov = plain.get("provenance")
    if isinstance(prov, dict) and prov.get("vision"):
        notes.append(f"vision provenance: {prov['vision']}")
    if plain.get("face_size_class"):
        notes.append(f"face_size_class: {plain['face_size_class']}")
    for note in plain.get("notes") or []:
        notes.append(str(note))
    return schema.ThumbnailAnalysis(
        text_word_count=plain.get("text_word_count"),
        text_role_notes=str(plain.get("text_role_notes", "") or ""),
        face_present=face,
        face_confidence=float(plain.get("face_confidence", 0.0) or 0.0),
        dominant_colours=colours,
        layout_class=str(plain.get("layout_class", "uncertain") or "uncertain"),
        contrast_class=str(plain.get("contrast_class", "uncertain") or "uncertain"),
        confidence=float(plain.get("confidence", 0.0) or 0.0),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Stage adapters — each returns a schema object (or raw probe dict)
# ---------------------------------------------------------------------------


def _stage_scenes(video_path: Path, probe_duration: Optional[float]) -> Any:
    mod, err = _import_sibling("scenes")
    if mod is None:
        raise _StageUnavailable("scenes", err)
    # Rich analysis first (scenes + aggregates + notes), then the
    # contract's detect_scenes list, then segmentation.
    rich = _first_callable(mod, ("analyse_video", "analyze_video"))
    if rich is not None:
        raw = _call_flexible(rich, ((str(video_path),), {}), ((video_path,), {}))
        return _to_schema_scenes(raw, probe_duration)
    fn = _first_callable(mod, ("detect_scenes", "segment_video", "detect", "find_scenes"))
    if fn is None:
        raise _StageUnavailable("scenes", None)
    raw = _call_flexible(fn, ((str(video_path),), {}), ((video_path,), {}), ((), {}))
    return _to_schema_scenes(raw, probe_duration)


def _stage_visual(video_path: Path, scene_analysis: Any) -> Any:
    mod, err = _import_sibling("visual_classify")
    if mod is None:
        raise _StageUnavailable("visual_classify", err)
    fn = _first_callable(
        mod,
        ("classify_scenes", "classify_visuals", "classify", "classify_video_scenes"),
    )
    if fn is None:
        raise _StageUnavailable("visual_classify", None)
    scenes_plain = _to_plain(getattr(scene_analysis, "scenes", []) or [])
    raw = _call_flexible(
        fn,
        ((str(video_path), scenes_plain), {}),
        ((video_path, scenes_plain), {}),
        ((str(video_path),), {}),
    )
    return _to_schema_visual(raw, scene_analysis)


def _stage_speech(video_path: Path, scene_analysis: Any, work_dir: Optional[Path]) -> Any:
    mod, err = _import_sibling("speech")
    if mod is None:
        raise _StageUnavailable("speech", err)
    scenes_plain = _to_plain(getattr(scene_analysis, "scenes", []) or [])
    full = _first_callable(mod, ("analyse_video_speech", "analyse_speech", "analyze_speech"))
    if full is not None:
        kwargs: dict[str, Any] = {}
        if work_dir is not None:
            kwargs["output_dir"] = str(work_dir / "_speech_work")
        raw = _call_flexible(
            full,
            ((str(video_path),), kwargs),
            ((str(video_path), scenes_plain), {}),
            ((video_path,), {}),
            ((str(video_path),), {}),
        )
        return _to_schema_speech(raw)
    transcribe_fn = _first_callable(mod, ("transcribe", "transcribe_video", "transcribe_audio"))
    if transcribe_fn is None:
        raise _StageUnavailable("speech", None)
    kwargs = {}
    if work_dir is not None:
        kwargs["output_dir"] = str(work_dir / "_speech_work")
    transcript = _call_flexible(
        transcribe_fn,
        ((str(video_path),), kwargs),
        ((str(video_path),), {}),
        ((video_path,), {}),
    )
    combined: dict[str, Any] = {}
    if isinstance(transcript, dict):
        combined.update(transcript)
    structure_fn = _first_callable(mod, ("analyse_structure", "analyze_structure"))
    if structure_fn is not None and isinstance(transcript, dict):
        try:
            structure = _call_flexible(
                structure_fn,
                ((transcript.get("segments"), transcript.get("duration_s")), {}),
                ((transcript,), {}),
            )
            if isinstance(structure, dict):
                combined.update(structure)
        except Exception as exc:
            combined["structure_error"] = {
                "error_class": exc.__class__.__name__,
                "error_message": _err_text(exc),
            }
    return _to_schema_speech(combined)


def _stage_dressing(video_path: Path, scene_analysis: Any, speech_obj: Any) -> Any:
    mod, err = _import_sibling("dressing")
    if mod is None:
        raise _StageUnavailable("dressing", err)
    fn = _first_callable(
        mod,
        ("analyse_dressing", "analyze_dressing", "analyse", "analyze", "detect_dressing"),
    )
    scenes_plain = _to_plain(getattr(scene_analysis, "scenes", []) or [])
    if fn is not None:
        raw = _call_flexible(
            fn,
            ((str(video_path), scenes_plain), {}),
            ((str(video_path),), {}),
            ((video_path, scenes_plain), {}),
            ((str(video_path), scenes_plain, _to_plain(speech_obj)), {}),
        )
        return _to_schema_dressing(raw)
    # Fallback: per-aspect functions (music / captions / overlays).
    aspect_fns = {
        "music": ("analyse_music", "analyze_music", "detect_music"),
        "captions": ("analyse_captions", "analyze_captions", "detect_captions"),
        "overlays": ("analyse_overlays", "analyze_overlays", "detect_overlays"),
    }
    combined: dict[str, Any] = {}
    found_any = False
    for aspect, names in aspect_fns.items():
        afn = _first_callable(mod, names)
        if afn is None:
            continue
        found_any = True
        try:
            combined[aspect] = _to_plain(
                _call_flexible(afn, ((str(video_path),), {}), ((video_path,), {}))
            )
        except Exception as exc:
            combined[aspect] = {
                "error_class": exc.__class__.__name__,
                "error_message": _err_text(exc),
            }
    if not found_any:
        raise _StageUnavailable("dressing", None)
    return _to_schema_dressing(combined)


def _stage_thumbnail(thumbnail_path: Path) -> Optional[Any]:
    mod, err = _import_sibling("thumbnail")
    if mod is None:
        raise _StageUnavailable("thumbnail", err)
    fn = _first_callable(
        mod, ("analyse_thumbnail", "analyze_thumbnail", "analyse", "analyze")
    )
    if fn is None:
        raise _StageUnavailable("thumbnail", None)
    raw = _call_flexible(fn, ((str(thumbnail_path),), {}), ((thumbnail_path,), {}))
    return _to_schema_thumbnail(raw)


# ---------------------------------------------------------------------------
# Visual mix aggregation helper (aggregate-level checks / fallback path)
# ---------------------------------------------------------------------------

_LABEL_KEYS = ("label", "classification", "category", "source_class", "kind", "verdict")


def _scene_label(scene: Any) -> Optional[str]:
    if isinstance(scene, dict):
        for key in _LABEL_KEYS:
            val = scene.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().lower()
            if isinstance(val, dict):
                inner = _scene_label(val)
                if inner:
                    return inner
    return None


def _scene_duration(scene: Any) -> Optional[float]:
    if isinstance(scene, dict):
        for key in ("duration_s", "duration", "length_s"):
            val = scene.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
        start = scene.get("start_s", scene.get("start"))
        end = scene.get("end_s", scene.get("end"))
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            return float(end - start)
    return None


def compute_visual_mix(
    classified_scenes: Any, probe_duration: Optional[float] = None
) -> dict[str, Any]:
    """Aggregate classified scenes into a label mix (counts + %).

    Returns per-scene and duration-weighted percentages. Scenes
    without a readable label count as ``unlabelled`` — never dropped.
    """
    scenes = _to_plain(classified_scenes) or []
    if isinstance(scenes, dict) and isinstance(scenes.get("scenes"), list):
        scenes = scenes["scenes"]
    if not isinstance(scenes, list):
        scenes = []
    counts: dict[str, int] = {}
    dur_by_label: dict[str, float] = {}
    total_dur = 0.0
    for scene in scenes:
        label = _scene_label(scene) or "unlabelled"
        counts[label] = counts.get(label, 0) + 1
        dur = _scene_duration(scene)
        if dur:
            dur_by_label[label] = dur_by_label.get(label, 0.0) + dur
            total_dur += dur
    n = len(scenes)
    pct = {k: round(100.0 * v / n, 2) for k, v in counts.items()} if n else {}
    pct_dur = (
        {k: round(100.0 * v / total_dur, 2) for k, v in dur_by_label.items()}
        if total_dur > 0
        else {}
    )
    return {
        "scene_count": n,
        "counts": counts,
        "pct_by_scene": pct,
        "pct_by_duration": pct_dur,
        "duration_accounted_s": round(total_dur, 3) if total_dur else None,
    }


# ---------------------------------------------------------------------------
# Persistence: storage-first, engine fallback
# ---------------------------------------------------------------------------


def _persist(
    result: Any, payload: dict[str, Any], out_dir: Path
) -> dict[str, Any]:
    """Persist via storage.py when possible; else engine fallback.

    Returns a persistence report dict (never raises)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"out_dir": str(out_dir), "via": None}
    storage_mod, _err = _import_sibling("storage")
    if storage_mod is not None and not isinstance(result, EngineAnalysisResult):
        fn = _first_callable(storage_mod, ("save_analysis", "save_result", "persist"))
        if fn is not None:
            try:
                stored = _call_flexible(
                    fn,
                    ((result,), {"base_dir": str(out_dir)}),
                    ((result, str(out_dir)), {}),
                    ((result,), {}),
                )
                plain = _to_plain(stored)
                report["via"] = "storage"
                report["stored"] = plain
                if isinstance(plain, dict):
                    report["analysis_json"] = plain.get("analysis_path")
                    report["breakdown_md"] = plain.get("breakdown_path")
                return report
            except Exception as exc:
                report["storage_error"] = {
                    "error_class": exc.__class__.__name__,
                    "error_message": _err_text(exc),
                }
    # Engine fallback persistence (schema JSON + provenance, rendered MD).
    try:
        json_path = out_dir / "analysis.json"
        json_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        md_text: Optional[str] = None
        render_mod, _ = _import_sibling("render")
        if render_mod is not None and not isinstance(result, EngineAnalysisResult):
            rfn = _first_callable(render_mod, ("render_breakdown", "render_markdown"))
            if rfn is not None:
                try:
                    md_text = _call_flexible(rfn, ((result,), {}))
                except Exception:
                    md_text = None
        if not isinstance(md_text, str):
            md_text = _fallback_markdown(payload)
        md_path = out_dir / "breakdown.md"
        md_path.write_text(md_text, encoding="utf-8")
        report["via"] = "engine_fallback"
        report["analysis_json"] = str(json_path)
        report["breakdown_md"] = str(md_path)
    except Exception as exc:
        report["via"] = "failed"
        report["error"] = {
            "error_class": exc.__class__.__name__,
            "error_message": _err_text(exc),
        }
    return report


def _fallback_markdown(payload: dict[str, Any]) -> str:
    video = payload.get("video") or {}
    lines = [
        "# Style Lab — Video Breakdown",
        "",
        f"- Video: `{video.get('path', '')}`",
        f"- Duration (s): {video.get('duration_s')}",
        "",
        "## Stage provenance",
        "",
        "| Stage | Status | Wall (s) | Error |",
        "|---|---|---|---|",
    ]
    for prov in payload.get("provenance", []):
        lines.append(
            f"| {prov.get('stage')} | {prov.get('status')} | "
            f"{prov.get('wall_s')} | {prov.get('error_class') or ''} |"
        )
    lines += [
        "", "## Visual mix (aggregate)", "", "```json",
        json.dumps(payload.get("visual_mix") or {}, indent=2, ensure_ascii=False),
        "```", "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _run_stage(
    provenance: list[StageProvenance],
    timings: dict[str, float],
    stage: str,
    fn: Callable[[], Any],
) -> tuple[Any, StageProvenance]:
    """Run one stage with timing + graceful degradation."""
    started = time.perf_counter()
    try:
        payload = fn()
        wall = time.perf_counter() - started
        prov = StageProvenance(stage=stage, status=STATUS_OK, wall_s=round(wall, 3))
    except _StageUnavailable as exc:
        wall = time.perf_counter() - started
        cause = exc.cause
        prov = StageProvenance(
            stage=stage,
            status=STATUS_STAGE_UNAVAILABLE,
            wall_s=round(wall, 3),
            error_class=cause.__class__.__name__ if cause is not None else "StageUnavailable",
            error_message=_err_text(cause) if cause is not None else "no usable entry point",
            detail="stage_unavailable: sibling module missing or exposes no usable entry point",
        )
        payload = None
    except Exception as exc:
        wall = time.perf_counter() - started
        prov = StageProvenance(
            stage=stage,
            status=STATUS_ERROR,
            wall_s=round(wall, 3),
            error_class=exc.__class__.__name__,
            error_message=_err_text(exc),
        )
        payload = None
    provenance.append(prov)
    timings[stage] = prov.wall_s
    return payload, prov


def result_payload(result: Any) -> dict[str, Any]:
    """Full payload dict for any engine result (schema or wrapper)."""
    payload = getattr(result, "__dict__", {}).get("_engine_payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(result, EngineAnalysisResult):
        return result.data
    plain = _to_plain(result)
    return plain if isinstance(plain, dict) else {"result": plain}


def analyse_video(
    video_path: str | Path,
    thumbnail_path: str | Path | None = None,
    metadata: dict[str, Any] | None = None,
    out_dir: str | Path | None = None,
) -> Any:
    """Run the full Style Lab analysis over ``video_path``.

    Parameters
    ----------
    video_path:
        Path to the video file to analyse.
    thumbnail_path:
        Optional thumbnail image; when given, the thumbnail stage runs.
    metadata:
        Optional caller metadata. ``competitor_video_id``,
        ``channel_id`` and ``source_url`` map onto the schema result;
        everything else is retained in the engine payload only.
    out_dir:
        When given, the assembled result is persisted there via
        ``storage.save_analysis`` (which writes ``analysis.json`` +
        ``breakdown.md`` under a per-video subdirectory plus an
        ``index.json``), with an engine fallback writer if storage is
        unavailable.

    Returns a ``schema.StyleLabAnalysis`` (with ``.provenance``,
    ``.stage_timings`` and ``.engine_payload`` attached) whenever the
    schema module is importable; otherwise an
    :class:`EngineAnalysisResult`. Stage failures never raise.
    """
    overall_started = time.perf_counter()
    vpath = Path(video_path)
    tpath = Path(thumbnail_path) if thumbnail_path else None
    meta = dict(metadata or {})
    provenance: list[StageProvenance] = []
    timings: dict[str, float] = {}
    work_dir = Path(out_dir) if out_dir else None

    schema_mod, schema_err = _import_sibling("schema")

    # -- probe -------------------------------------------------------------
    probe_payload, _ = _run_stage(
        provenance, timings, "probe", lambda: probe_video(vpath)
    )
    probe = probe_payload if isinstance(probe_payload, dict) else {}

    # -- scenes ------------------------------------------------------------
    scenes_obj, scenes_prov = _run_stage(
        provenance, timings, "scenes",
        lambda: _stage_scenes(vpath, probe.get("duration_s")),
    )

    # -- visual classification --------------------------------------------
    visual_obj, visual_prov = _run_stage(
        provenance, timings, "visual_classify",
        lambda: _stage_visual(vpath, scenes_obj),
    )

    # -- speech -------------------------------------------------------------
    speech_obj, speech_prov = _run_stage(
        provenance, timings, "speech",
        lambda: _stage_speech(vpath, scenes_obj, work_dir),
    )

    # -- dressing -----------------------------------------------------------
    dressing_obj, dressing_prov = _run_stage(
        provenance, timings, "dressing",
        lambda: _stage_dressing(vpath, scenes_obj, speech_obj),
    )

    # -- thumbnail (optional) ------------------------------------------------
    if tpath is not None:
        thumbnail_obj, thumb_prov = _run_stage(
            provenance, timings, "thumbnail", lambda: _stage_thumbnail(tpath)
        )
    else:
        thumb_prov = StageProvenance(
            stage="thumbnail", status=STATUS_SKIPPED, wall_s=0.0,
            detail="no thumbnail_path supplied",
        )
        provenance.append(thumb_prov)
        timings["thumbnail"] = 0.0
        thumbnail_obj = None

    # -- assemble -------------------------------------------------------------
    notes: list[str] = []
    for prov in provenance:
        if prov.status in (STATUS_STAGE_UNAVAILABLE, STATUS_ERROR):
            notes.append(
                f"stage {prov.stage}: {prov.status}"
                + (f" ({prov.error_class})" if prov.error_class else "")
            )

    payload: dict[str, Any] = {
        "video_path": str(vpath),
        "probe": probe,
        "source_metadata": meta,
        "scenes": _to_plain(scenes_obj),
        "visual_mix": _to_plain(visual_obj),
        "speech": _to_plain(speech_obj),
        "dressing": _to_plain(dressing_obj),
        "thumbnail": _to_plain(thumbnail_obj),
        "thumbnail_path": str(tpath) if tpath else None,
        "provenance": [p.to_dict() for p in provenance],
        "stage_timings": dict(timings),
        "notes": notes,
    }

    result: Any
    if schema_mod is not None:
        schema = schema_mod
        video_meta = schema.VideoMetadata(
            path=str(vpath),
            duration_s=probe.get("duration_s"),
            width=probe.get("width"),
            height=probe.get("height"),
            fps=probe.get("fps"),
            codec=probe.get("video_codec"),
            file_size_bytes=probe.get("size_bytes"),
        )
        scenes_final = scenes_obj if scenes_obj is not None else schema.SceneAnalysis.make_uncertain(
            [f"scene stage: {scenes_prov.status}"]
        )
        visual_final = visual_obj if visual_obj is not None else schema.VisualMix.make_uncertain(
            [f"visual stage: {visual_prov.status}"]
        )
        speech_final = speech_obj if speech_obj is not None else schema.SpeechAnalysis.make_uncertain(
            [f"speech stage: {speech_prov.status}"]
        )
        dressing_final = dressing_obj if dressing_obj is not None else schema.DressingAnalysis.make_uncertain(
            [f"dressing stage: {dressing_prov.status}"]
        )
        confidences = []
        for obj, prov in (
            (visual_obj, visual_prov),
            (speech_obj, speech_prov),
            (dressing_obj, dressing_prov),
            (thumbnail_obj, thumb_prov),
        ):
            if obj is not None and prov.status == STATUS_OK:
                confidences.append(float(getattr(obj, "confidence", 0.0) or 0.0))
        overall = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
        result = schema.StyleLabAnalysis(
            competitor_video_id=meta.get("competitor_video_id"),
            channel_id=meta.get("channel_id"),
            source_url=meta.get("source_url"),
            video=video_meta,
            scenes=scenes_final,
            visual_mix=visual_final,
            speech=speech_final,
            dressing=dressing_final,
            thumbnail=thumbnail_obj,
            overall_confidence=overall,
            notes=notes,
        )
        payload["analysis"] = _to_plain(result)
        payload["visual_mix_summary"] = (
            result.visual_mix.percentages()
            if hasattr(result.visual_mix, "percentages")
            else {}
        )
    else:
        payload["schema_error"] = {
            "error_class": schema_err.__class__.__name__ if schema_err else None,
            "error_message": _err_text(schema_err) if schema_err else None,
        }
        result = EngineAnalysisResult(data=payload)

    payload["total_wall_s"] = round(time.perf_counter() - overall_started, 3)
    payload["stage_timings"] = dict(timings)

    # Attach engine bookkeeping (schema deliberately carries none).
    for attr, value in (
        ("provenance", payload["provenance"]),
        ("stage_timings", dict(timings)),
        ("engine_payload", payload),
        ("_engine_payload", payload),
    ):
        try:
            object.__setattr__(result, attr, value)
        except Exception:
            pass
    if isinstance(result, EngineAnalysisResult):
        result.data = payload

    # -- persist ---------------------------------------------------------------
    if out_dir is not None:
        persist_started = time.perf_counter()
        report = _persist(result, payload, Path(out_dir))
        payload["persistence"] = report
        timings["persist"] = round(time.perf_counter() - persist_started, 3)
        payload["stage_timings"] = dict(timings)
        try:
            object.__setattr__(result, "stage_timings", dict(timings))
        except Exception:
            pass

    return result


#: US-spelling alias.
analyze_video = analyse_video
