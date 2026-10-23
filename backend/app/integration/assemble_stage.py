"""ROUND 2 integration — Worker 5: assembly stage.

Renders the final video from the upstream stage outputs and produces the
complete artefact pack:

    segments (render pipeline, Ken Burns on stills)
      -> concat + quality-tier encode + mux with the mastered audio
         (``quality_tier`` param: draft 720p default / master_1080p /
         master_1440p_upscale — Round 5)
      -> subtitles (.srt sidecar, alignment/stub provenance marked)
      -> 3 thumbnail variants (Pillow builder)
      -> technical QC on the final MP4 (verdicts taken verbatim)
      -> artefacts copied into ``ctx.pack_dir``

Upstream inputs (``bundle`` / ``voice`` / ``visuals`` / ``profile`` /
``ctx``) are produced by sibling Round-2 workers; this module reads them
defensively (objects or mappings, several alias names per field) so the
stage works against any reasonable shape those workers emit, while every
heavy operation goes through the REAL pipeline APIs:

* render:  :class:`SegmentRenderer`, :func:`render_draft`,
  :class:`SceneSpec` / :class:`SourceType` (contract.py)
* subtitles: :func:`build_subtitles` + :func:`write_srt_sidecar`
  (service.py).  When no TTS word timings exist, the step now runs the
  REAL faster-whisper transcriber (provider factory
  :func:`create_transcriber`) on the narration audio; the alignment
  stub is kept ONLY as an explicit fallback for when the model /
  transcriber is unavailable — provenance is NEVER hidden either way
  (``whisper`` vs ``alignment_stub``).
* thumbnails: :func:`generate_variants` (variants.py), 3 variants.
  When a Style Lab style profile resolves for the spec
  (:func:`app.integration.style_profile.resolve_style_profile`), its
  thumbnail directives are mapped onto the channel profile
  (:mod:`app.integration.style_thumbnail` — style profile wins, niche
  profile is the fallback), the headline is band-checked against the
  profile's text-word band, and the applied parameters are recorded
  under ``style_applied`` on the result.  No profile -> unchanged.
* QC: :func:`run_technical_qc` + :class:`MediaContract` (technical.py);
  the measured loudness is reported as-is, pass/fail follows the QC
  module's own verdict.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

# -- render pipeline ---------------------------------------------------------
from app.pipeline.render.contract import FPS, SceneSpec, SourceType
from app.pipeline.render.encoder import probe_encoder
from app.pipeline.render.qc import probe_duration
from app.pipeline.render.renderer import (
    SegmentRenderer,
    quality_tier_spec,
    render_draft,
    render_master,
    resolve_quality_tier,
)

# -- subtitles ---------------------------------------------------------------
from app.pipeline.subtitles.models import (
    SOURCE_ALIGNMENT_STUB,
    CaptionsProfile,
)
from app.pipeline.subtitles.service import build_subtitles, write_srt_sidecar
from app.pipeline.subtitles.whisper_wrapper import WhisperTranscriber

# -- real transcription provider (faster-whisper) ---------------------------
from app.providers.transcription import create_transcriber

# -- thumbnails ---------------------------------------------------------------
from app.pipeline.thumbnail.models import ChannelStyleProfile, default_profile
from app.pipeline.thumbnail.variants import generate_variants
from app.ffmpeg_resolution import ffmpeg_executable

# -- Style Lab style profiles (W1 module; defensive import so this
#    stage still runs unstyled if the module is unavailable) -----------
try:
    from app.integration.style_profile import (
        resolve_style_profile,
        style_directives,
    )
except Exception:  # pragma: no cover - module unavailable
    resolve_style_profile = None  # type: ignore[assignment]
    style_directives = None  # type: ignore[assignment]

from app.integration.style_thumbnail import (
    adjust_headlines,
    apply_style_thumbnail,
    extract_thumbnail_directives,
)

# -- technical QC --------------------------------------------------------------
from app.pipeline.gates.tokens import Verdict
from app.pipeline.qc.technical import MediaContract, TimeRange, run_technical_qc

__all__ = ["AssembleResult", "assemble_project"]

_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".ts", ".m4v", ".avi"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_SOLID_COLORS = ("0x101418", "0x1a2233", "0x14231a", "0x231a2e", "0x2e1a1a")

# -- whisper defaults ---------------------------------------------------------
#: Env var that overrides the faster-whisper model size for this stage.
WHISPER_MODEL_SIZE_ENV_VAR = "YTA_WHISPER_MODEL_SIZE"
#: Default faster-whisper model for the integration subtitles step.
#: Evidence (Round-3 validation on the Round-2 facts video — Urdu
#: narration, 171 known words, audio extracted from final.mp4):
#: ``tiny`` recovered only 81/171 words (consonant-skeleton WER 0.852);
#: ``base`` recovered 95/171 (0.840) and was the smallest tested model
#: that was measurably better than tiny on Urdu, so ``base`` is the
#: default.  (Both models' strict, script-sensitive WER against the
#: Roman-Urdu script was 1.000 — whisper emits Urdu script; see the
#: validation artefacts under outputs/round3/whisper_wer/.)
DEFAULT_WHISPER_MODEL_SIZE = "base"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class AssembleResult:
    """Contract result of the assembly stage (Worker 5)."""

    final_mp4: str
    duration_s: float
    captions_srt: str
    #: Which subtitle path produced the captions: "alignment-stub" when
    #: the stub/alignment fallback was used, otherwise the real source
    #: ("alignment" / "whisper").
    subtitle_path_used: str
    thumbnails: list[str]
    qc_passed: bool
    qc_summary: dict
    loudness_lufs: Optional[float]
    render_path_used: str = "draft"
    #: Round 5 master encode: the resolved output quality tier
    #: ("draft" / "master_1080p" / "master_1440p_upscale").  Mirrors
    #: ``render_path_used`` (which now carries the tier name); kept as
    #: its own field so report consumers do not parse paths_used.
    quality_tier: str = "draft"
    #: Wall-clock seconds the final tier encode (concat + scale/encode
    #: + mux of the mastered audio) took.
    render_wall_time_s: float = 0.0
    #: Size in bytes of the final output MP4 in the pack.
    output_file_size_bytes: int = 0
    # -- extras (beyond the contract; used by the publish stage) -------------
    subtitle_source: str = ""
    subtitle_warnings: list[str] = field(default_factory=list)
    segment_paths: list[str] = field(default_factory=list)
    #: Per-scene licence/provenance rows for what was actually rendered
    #: (the project's licence-ledger view the copyright gate joins over).
    licence_segments: list[dict] = field(default_factory=list)
    pack_dir: str = ""
    work_dir: str = ""
    qc_report: Any = None  # app.pipeline.qc.technical.QCReport
    notes: dict = field(default_factory=dict)
    #: Style Lab parameters actually applied by this stage, recorded
    #: under the shared "style_applied" key: {"thumbnail": {...}} when
    #: a style profile resolved, {} when the stage ran unstyled.
    style_applied: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Defensive accessors for sibling-worker shapes
# ---------------------------------------------------------------------------


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """First non-None value among attribute / mapping-key ``names``."""
    if obj is None:
        return default
    for name in names:
        value: Any = None
        if isinstance(obj, Mapping):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _nested(obj: Any, *names: str) -> Any:
    """Like :func:`_get` but also looks one level into metadata/seo/spec."""
    value = _get(obj, *names)
    if value is not None:
        return value
    for container_name in ("metadata", "seo", "spec", "meta"):
        container = _get(obj, container_name)
        if container is not None:
            value = _get(container, *names)
            if value is not None:
                return value
    return None


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(v) for v in value if v is not None and str(v).strip()]
    return [str(value)]


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (Mapping, list, tuple)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_duration(path: Path) -> Optional[float]:
    try:
        value = probe_duration(Path(path))
        return float(value) if value and float(value) > 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Input resolution
# ---------------------------------------------------------------------------


def _resolve_pack_and_work(ctx: Any) -> tuple[Path, Path]:
    pack = _get(ctx, "pack_dir", "package_dir", "output_dir", "out_dir", "dir")
    if pack is None:
        raise ValueError("ctx carries no pack_dir (or output_dir) to write the pack into")
    pack_dir = Path(str(pack))
    pack_dir.mkdir(parents=True, exist_ok=True)
    work = _get(ctx, "work_dir", "scratch_dir", "tmp_dir", "workdir")
    work_dir = Path(str(work)) if work is not None else pack_dir / "_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return pack_dir, work_dir


def _resolve_audio(voice: Any) -> tuple[Path, Optional[float]]:
    """The already-mixed master from the voice stage."""
    candidate = None
    if isinstance(voice, (str, Path)):
        candidate = voice
    else:
        candidate = _get(
            voice,
            "mixed_master_wav", "master_wav", "mixed_path", "mixed_master",
            "narration_wav", "master_path", "audio_path", "wav_path",
            "output_path", "path", "file",
        )
    if candidate is None:
        raise ValueError(
            "voice stage result carries no master audio path "
            "(looked for mixed_master_wav / narration_wav / audio_path / ...)"
        )
    audio_path = Path(str(candidate))
    if not audio_path.is_file():
        raise ValueError(f"master audio file does not exist: {audio_path}")
    duration = _probe_duration(audio_path)
    if duration is None:
        raw = _get(voice, "duration_s", "total_duration_s", "duration")
        duration = float(raw) if raw else None
    return audio_path, duration


def _resolve_script(bundle: Any, voice: Any) -> tuple[str, list[str]]:
    sentences = _as_str_list(
        _get(bundle, "script_sentences", "sentences", "lines")
        or _get(voice, "script_sentences", "sentences")
    )
    script_text = _first_text(
        _nested(bundle, "script_text", "script", "full_script", "narration_text", "text"),
        _get(voice, "script_text", "script", "text", "transcript"),
        " ".join(sentences),
    )
    if not sentences and script_text:
        from app.pipeline.subtitles.srt_builder import split_script_sentences

        sentences = split_script_sentences(script_text)
    return script_text, sentences


def _captions_profile(profile: Any, bundle: Any) -> CaptionsProfile:
    raw = _first_text(
        _get(profile, "captions_profile", "caption_profile"),
        _nested(bundle, "captions_profile", "caption_profile"),
        _get(profile, "language"),
        _nested(bundle, "language", "caption_language"),
    ).lower()
    if raw in ("english", "en", "en-us", "en_us"):
        return CaptionsProfile.ENGLISH
    if raw in ("roman_urdu", "roman-urdu"):
        return CaptionsProfile.ROMAN_URDU
    if raw.startswith("roman"):
        return CaptionsProfile.ROMAN_URDU
    if "urdu" in raw:
        return CaptionsProfile.URDU_SCRIPT
    try:
        return CaptionsProfile.coerce(raw)
    except Exception:
        return CaptionsProfile.ENGLISH


# ---------------------------------------------------------------------------
# Subtitles transcription (Round 3: REAL whisper first, stub fallback)
# ---------------------------------------------------------------------------


def _whisper_language(language: Any, profile: CaptionsProfile) -> Optional[str]:
    """Normalise a stage language label to a faster-whisper language code.

    Stage/profile labels are NOT whisper codes (``roman_urdu``,
    ``Urdu``, ``ur-PK`` …) and faster-whisper rejects unknown codes, so
    map the known labels, pass plain 2-letter codes through, fall back
    to the captions profile, and otherwise return ``None`` (whisper
    auto-detects).
    """
    raw = str(language or "").strip().lower().replace("-", "_")
    if raw in ("en", "en_us", "en_gb", "english"):
        return "en"
    if raw in ("ur", "ur_pk", "urdu", "roman_urdu"):
        return "ur"
    if len(raw) == 2 and raw.isalpha():
        return raw
    if profile in (CaptionsProfile.URDU_SCRIPT, CaptionsProfile.ROMAN_URDU):
        return "ur"
    if profile is CaptionsProfile.ENGLISH:
        return "en"
    return None


def _resolve_transcription_audio(voice: Any, master_audio: Path) -> Path:
    """Audio the transcriber listens to: the clean narration stem when
    the voice stage provides one (no music bed under the speech),
    otherwise the assembled master.  Both share the master timeline."""
    candidate = _get(voice, "narration_wav", "narration_path")
    if candidate is not None:
        path = Path(str(candidate))
        if path.is_file():
            return path
    return master_audio


def _transcribe_for_subtitles(
    audio_path: Path,
    *,
    language: Optional[str],
    script_text: str,
    duration_s: Optional[float],
    notes: dict,
) -> list:
    """Transcribe ``audio_path`` for the subtitles step.

    REAL faster-whisper is the default: the provider factory
    (:func:`create_transcriber`) builds a non-stub transcriber, and the
    script text is handed to it so that — if the model genuinely cannot
    load or run — its documented internal fallback derives
    alignment-stub segments instead of crashing.  ``stub_mode=None``
    is passed explicitly so the ``YTA_SUBTITLES_STUB`` env kill-switch
    still forces the stub when an operator sets it.

    The alignment stub therefore remains ONLY as an explicit fallback:
    every returned segment carries its provenance in ``segment.source``
    (``"whisper"`` vs ``"alignment_stub"``), and the transcriber's
    ``last_provenance`` / ``last_error`` are mirrored into ``notes``.
    Returns ``[]`` when no timing source exists at all (the caller then
    applies its last-resort script-duration spread, also stub-marked).
    """
    model_size = (
        (os.environ.get(WHISPER_MODEL_SIZE_ENV_VAR) or "").strip()
        or DEFAULT_WHISPER_MODEL_SIZE
    )
    notes["subtitle_whisper_model"] = model_size
    transcriber: Any = None
    try:
        transcriber = create_transcriber(
            model_size,
            script_text=script_text or None,
            stub_mode=None,
        )
    except Exception as exc:  # factory itself unavailable -> explicit stub
        notes["subtitle_whisper_error"] = (
            f"transcriber factory failed: {type(exc).__name__}: {exc}"
        )
    if transcriber is None:
        transcriber = WhisperTranscriber(
            stub_mode=True,
            script_text=script_text or None,
        )
    try:
        segments = transcriber.transcribe(
            audio_path,
            language=language,
            script_text=script_text or None,
            duration_s=duration_s,
        )
    except Exception as exc:  # stub had no timing source at all
        notes["subtitle_stub_error"] = f"{type(exc).__name__}: {exc}"
        return []
    notes["subtitle_transcriber_provenance"] = getattr(
        transcriber, "last_provenance", None)
    last_error = getattr(transcriber, "last_error", None)
    if last_error:
        if notes.get("subtitle_whisper_error"):
            notes["subtitle_whisper_error"] += f"; transcriber: {last_error}"
        else:
            notes["subtitle_whisper_error"] = str(last_error)
    return list(segments or [])


def _normalise_visual_item(item: Any, index: int) -> dict:
    """Normalise one visual (path / scene record / download / mapping)."""
    out: dict[str, Any] = {
        "scene_id": f"s{index + 1:03d}",
        "path": None,
        "image_obj": None,
        "start_s": None,
        "end_s": None,
        "duration_s": None,
        "licence_id": "",
        "asset_hash": "",
        "asset_id": "",
        "provider": "",
        "motion_kind": "",
        "source_sentence": "",
    }
    if item is None:
        return out
    if isinstance(item, (str, Path)):
        out["path"] = str(item)
        return out
    # PIL image handed over directly.
    if hasattr(item, "save") and hasattr(item, "size"):
        out["image_obj"] = item
        return out
    # DownloadedAsset-like: .local_path + .candidate.
    candidate = _get(item, "candidate")
    if candidate is not None and _get(item, "local_path"):
        out["path"] = str(_get(item, "local_path"))
        out["asset_hash"] = _first_text(_get(item, "file_sha256", "sha256"))
        out["provider"] = _first_text(_get(candidate, "provider"))
        out["licence_id"] = _first_text(_get(candidate, "licence_id"))
        asset_id = _first_text(_get(candidate, "provider_asset_id"))
        if asset_id:
            out["asset_id"] = asset_id
        dur = _get(item, "duration_s") or _get(candidate, "duration_s")
        out["duration_s"] = float(dur) if dur else None
        return out

    path = _get(
        item, "local_path", "path", "file_path", "filepath", "asset_path",
        "image_path", "clip_path", "file",
    )
    if path is None:
        # SceneRecord-like: asset_ref may be a path (or an id — handled by
        # the caller's index-zip fallback when it is not a real file).
        path = _get(item, "asset_ref", "asset", "source_path", "url")
    if path is not None:
        out["path"] = str(path)
    out["scene_id"] = _first_text(_get(item, "scene_id", "id"), out["scene_id"]) or out["scene_id"]
    start = _get(item, "start_s", "start")
    end = _get(item, "end_s", "end")
    out["start_s"] = float(start) if start is not None else None
    out["end_s"] = float(end) if end is not None else None
    dur = _get(item, "duration_s", "duration")
    out["duration_s"] = float(dur) if dur else None
    out["licence_id"] = _first_text(
        _get(item, "licence_ref", "licence_id", "license_id", "license_ref"))
    out["asset_hash"] = _first_text(
        _get(item, "content_hash", "asset_hash", "file_sha256", "sha256", "hash"))
    out["asset_id"] = _first_text(
        _get(item, "asset_id", "provider_asset_id", "registry_id"))
    out["provider"] = _first_text(
        _get(item, "provider", "source_provider", "source"))
    motion = _get(item, "motion")
    if motion is not None:
        kind = _get(motion, "kind", "value") if not isinstance(motion, str) else motion
        out["motion_kind"] = str(getattr(kind, "value", kind) or "")
    out["source_sentence"] = _first_text(
        _get(item, "source_sentence", "narration_text", "sentence", "text"))
    return out


def _extract_visual_items(visuals: Any) -> list[dict]:
    """Pull the ordered scene/visual list out of the visuals stage result."""
    if visuals is None:
        return []
    if isinstance(visuals, (str, Path)) or hasattr(visuals, "save"):
        return [_normalise_visual_item(visuals, 0)]
    raw_list: Any = None
    if isinstance(visuals, Sequence) and not isinstance(visuals, Mapping):
        raw_list = list(visuals)  # type: ignore[arg-type]
    else:
        for key in ("scenes", "items", "assets", "files", "paths", "stills",
                    "clips", "segments", "results", "visuals", "media"):
            value = _get(visuals, key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                raw_list = list(value)  # type: ignore[arg-type]
                break
        if raw_list is None:
            manifest = _get(visuals, "manifest", "scene_manifest")
            scenes = _get(manifest, "scenes") if manifest is not None else None
            if isinstance(scenes, Sequence):
                raw_list = list(scenes)  # type: ignore[arg-type]
        if raw_list is None and (
            _get(visuals, "path", "local_path", "asset_ref", "file") is not None
        ):
            raw_list = [visuals]
    if not raw_list:
        return []
    items = [_normalise_visual_item(item, i) for i, item in enumerate(raw_list)]
    # Scene records whose asset_ref is an id, not a file: fall back to a
    # parallel path list on the visuals object, zipped by index.
    needs_path = [it for it in items if not it["path"] or not Path(it["path"]).exists()]
    if needs_path and not isinstance(visuals, Sequence):
        for key in ("asset_paths", "file_paths", "paths", "files", "downloads"):
            pool = _get(visuals, key)
            if isinstance(pool, Sequence) and not isinstance(pool, (str, bytes)):
                pool_items = [
                    _normalise_visual_item(p, i) for i, p in enumerate(pool)
                ]
                for idx, item in enumerate(items):
                    if idx < len(pool_items) and pool_items[idx]["path"]:
                        if not item["path"] or not Path(item["path"]).exists():
                            item["path"] = pool_items[idx]["path"]
                            item["asset_hash"] = item["asset_hash"] or pool_items[idx]["asset_hash"]
                            item["provider"] = item["provider"] or pool_items[idx]["provider"]
                break
    return items


def _plan_durations(
    items: list[dict], audio_duration: Optional[float]
) -> list[float]:
    """Frame-aligned per-scene durations summing to the audio duration."""
    raw: list[Optional[float]] = []
    for item in items:
        if item["start_s"] is not None and item["end_s"] is not None:
            raw.append(max(0.1, float(item["end_s"]) - float(item["start_s"])))
        elif item["duration_s"]:
            raw.append(max(0.1, float(item["duration_s"])))
        else:
            raw.append(None)
    n = len(items)
    if audio_duration and audio_duration > 0:
        known = sum(r for r in raw if r is not None)
        missing = sum(1 for r in raw if r is None)
        if missing:
            share = max(0.5, (audio_duration - known) / missing) if audio_duration > known else 0.5
            raw = [r if r is not None else share for r in raw]
        total = sum(r or 0.0 for r in raw)
        if total > 0:
            scale = audio_duration / total
            raw = [(r or 0.0) * scale for r in raw]
        target_frames = int(round(audio_duration * FPS))
        frames = [max(1, int(round((r or 0.0) * FPS))) for r in raw]
        frames[-1] += target_frames - sum(frames)
        if frames[-1] < 1:
            frames[-1] = 1
        return [f / FPS for f in frames]
    # No audio duration to lock to: use what we have, 5 s default.
    filled = [r if r is not None else 5.0 for r in raw] or [5.0] * max(n, 1)
    return [int(round(d * FPS)) / FPS for d in filled]


def _thumbnail_profile(profile: Any) -> ChannelStyleProfile:
    if isinstance(profile, ChannelStyleProfile):
        return profile
    nested = _get(profile, "thumbnail_profile", "channel_style", "style_profile")
    if isinstance(nested, ChannelStyleProfile):
        return nested
    if isinstance(profile, Mapping) and profile.get("palette"):
        try:
            return ChannelStyleProfile(
                name=str(profile.get("name", "channel")),
                palette=list(profile["palette"]),
                accent_color=profile.get("accent_color"),
                font_path_latin=profile.get("font_path_latin"),
                font_path_nastaliq=profile.get("font_path_nastaliq"),
            )
        except Exception:
            pass
    if nested is not None and isinstance(nested, Mapping) and nested.get("palette"):
        try:
            return ChannelStyleProfile(
                name=str(nested.get("name", "channel")),
                palette=list(nested["palette"]),
            )
        except Exception:
            pass
    return default_profile()


# ---------------------------------------------------------------------------
# Style Lab: style-profile resolution for the thumbnail step
# ---------------------------------------------------------------------------


def _style_base_dir(ctx: Any, pack_dir: Path, work_dir: Path) -> Path:
    """The projects base dir the style resolver expects.

    ``resolve_style_profile(spec, base_dir)`` looks for the channel
    default at ``<base_dir>/style_profiles/<channel_id>.json``.  In
    the real pipeline ``ctx`` is a StageContext whose ``root`` is
    ``<base_dir>/<project_id>/``, so the base is ``root.parent``;
    an explicit base attribute wins, and the pack dir
    (``<base>/<project_id>/pack``) is the last-resort derivation.
    """
    explicit = _get(ctx, "base_dir", "projects_base", "work_base_dir", "base")
    if explicit:
        return Path(str(explicit))
    root = _get(ctx, "root", "root_dir", "project_dir")
    if root:
        return Path(str(root)).parent
    return pack_dir.parent.parent


def _resolve_style_thumbnail_directives(
    ctx: Any, bundle: Any, pack_dir: Path, work_dir: Path
) -> Optional[dict]:
    """Thumbnail directives of the resolved style profile, or None.

    Defensive by design: the spec is read via getattr (it gains
    ``style_profile_path`` from W2's models change), the resolver is
    the shared W1 contract, and a missing resolver module simply
    means "unstyled".  The resolver's own fail-loud behaviour for an
    explicitly configured but missing profile path (FileNotFoundError,
    R4-003) is intentionally NOT swallowed.
    """
    if resolve_style_profile is None:
        return None
    spec = _get(ctx, "spec") or _get(bundle, "spec")
    resolved = resolve_style_profile(spec, _style_base_dir(ctx, pack_dir, work_dir))
    if resolved is None:
        return None
    directives: Any = None
    if style_directives is not None:
        try:
            directives = style_directives(resolved)
        except Exception:
            directives = None
    return extract_thumbnail_directives(
        directives if directives is not None else resolved)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def assemble_project(bundle: Any, voice: Any, visuals: Any, profile: Any, ctx: Any,
                     quality_tier: Optional[str] = None) -> AssembleResult:
    """Assemble the final video + artefact pack (see module docstring).

    ``quality_tier`` is the Round-5 output tier (``"draft"`` /
    ``"master_1080p"`` / ``"master_1440p_upscale"``).  When it is not
    passed (``None``), the tier resolves from the context — the
    per-channel plumbing seam: ``ctx.spec.quality_tier`` first, then
    ``ctx.quality_tier`` — and ultimately defaults to ``"draft"``, so
    every pre-Round-5 caller reproduces the historical 720p behaviour
    exactly.  An explicit argument always wins over the context.  An
    unknown tier name raises :class:`ValueError` (fail loud).  Only
    the final encode's resolution / encoder parameters change with
    the tier: the mastered (loudnorm) audio, the subtitle sidecar
    and thumbnail generation are identical across tiers, and the QC
    module's thresholds are untouched — its expected-resolution
    contract simply follows the tier actually rendered.
    """
    notes: dict[str, Any] = {}
    if quality_tier is None:
        quality_tier = (
            _get(_get(ctx, "spec"), "quality_tier")
            or _get(ctx, "quality_tier")
            or "draft"
        )
    resolved_tier = resolve_quality_tier(quality_tier)
    tier_spec = quality_tier_spec(resolved_tier)
    notes["quality_tier"] = resolved_tier
    pack_dir, work_dir = _resolve_pack_and_work(ctx)
    audio_path, audio_duration = _resolve_audio(voice)
    script_text, script_sentences = _resolve_script(bundle, voice)

    items = _extract_visual_items(visuals)
    if not items:
        notes["visuals_fallback"] = "no visuals supplied; single solid segment used"
        items = [_normalise_visual_item(None, 0)]
    durations = _plan_durations(items, audio_duration)
    total_planned = sum(durations)

    # -- persist PIL images handed over directly ------------------------------
    stills_dir = work_dir / "stills"
    for idx, item in enumerate(items):
        if item["image_obj"] is not None:
            stills_dir.mkdir(parents=True, exist_ok=True)
            target = stills_dir / f"scene_{idx + 1:03d}.png"
            item["image_obj"].save(str(target))
            item["path"] = str(target)

    # -- build segment specs ----------------------------------------------------
    encoder_choice = probe_encoder()
    notes["encoder"] = encoder_choice.encoder
    renderer = SegmentRenderer(work_dir / "segments", encoder_choice=encoder_choice)

    specs: list[SceneSpec] = []
    licence_segments: list[dict] = []
    declared_still_ranges: list[TimeRange] = []
    cursor = 0.0
    for idx, (item, dur) in enumerate(zip(items, durations)):
        scene_id = item["scene_id"] or f"s{idx + 1:03d}"
        path = Path(item["path"]) if item["path"] else None
        source_type = SourceType.SOLID
        source_path = ""
        if path is not None and path.is_file():
            suffix = path.suffix.lower()
            if suffix in _VIDEO_SUFFIXES:
                source_type = SourceType.VIDEO
                source_path = str(path)
            elif suffix in _IMAGE_SUFFIXES:
                source_type = SourceType.STILL
                source_path = str(path)
            else:
                notes.setdefault("unknown_suffix_scenes", []).append(scene_id)
        elif path is not None:
            notes.setdefault("missing_asset_scenes", []).append(scene_id)

        asset_hash = item["asset_hash"]
        if source_path and not asset_hash:
            try:
                asset_hash = _file_sha256(Path(source_path))
            except OSError:
                asset_hash = ""

        ken_burns = True
        zoom_start, zoom_end = 1.0, 1.12
        motion = (item["motion_kind"] or "").lower()
        if motion in ("zoom_out", "zoomout"):
            zoom_start, zoom_end = 1.12, 1.0
        elif motion in ("static", "none"):
            ken_burns = False
        offset = _get(item, "start_offset_seconds", "trim_start_s", "source_offset")
        specs.append(
            SceneSpec(
                scene_id=scene_id,
                source_type=source_type,
                duration_seconds=dur,
                source_path=source_path,
                asset_hash=asset_hash,
                licence_id=item["licence_id"],
                start_offset_seconds=float(offset) if offset else 0.0,
                color=_SOLID_COLORS[idx % len(_SOLID_COLORS)],
                ken_burns=ken_burns,
                zoom_start=zoom_start,
                zoom_end=zoom_end,
            )
        )
        if source_type is SourceType.STILL:
            declared_still_ranges.append(TimeRange(start_s=cursor, end_s=cursor + dur))
        licence_segments.append(
            {
                "asset_id": item["asset_id"] or scene_id,
                "scene_id": scene_id,
                "start_s": round(cursor, 3),
                "end_s": round(cursor + dur, 3),
                "licence_id": item["licence_id"] or f"OWN-{scene_id}",
                "provider": item["provider"] or "generated",
                "content_hash": asset_hash,
                "source_type": source_type.value,
                "role": "b_roll",
            }
        )
        cursor += dur

    segment_paths = renderer.render_all(specs)

    # -- tier render (concat + scale + mux mastered audio, one pass) -------
    # Segments are always the locked 1080p timeline; the tier decides
    # only the final scale/encode.  Draft goes through render_draft
    # (the historical path, byte-for-byte the old behaviour); the
    # master tiers go through render_master (upscale=True is exactly
    # master_1440p_upscale, upscale=False is master_1080p).
    render_started = time.monotonic()
    render_work = work_dir / f"{resolved_tier}.mp4"
    if resolved_tier == "draft":
        render_draft(
            segment_paths, render_work,
            audio=audio_path, encoder_choice=encoder_choice,
        )
    else:
        render_master(
            segment_paths, render_work,
            audio=audio_path,
            upscale=resolved_tier == "master_1440p_upscale",
            encoder_choice=encoder_choice,
        )
    final_mp4 = pack_dir / "final.mp4"
    shutil.copy2(render_work, final_mp4)
    render_wall_time_s = round(time.monotonic() - render_started, 3)
    output_file_size_bytes = (
        final_mp4.stat().st_size if final_mp4.is_file() else 0
    )
    notes["render"] = {
        "quality_tier": resolved_tier,
        "width": tier_spec.width,
        "height": tier_spec.height,
        "render_wall_time_s": render_wall_time_s,
        "output_file_size_bytes": output_file_size_bytes,
    }
    duration_s = _probe_duration(final_mp4) or total_planned

    # -- subtitles (alignment happy path; else REAL whisper on the narration
    #    audio, alignment stub only as explicit fallback — provenance kept)
    captions_profile = _captions_profile(profile, bundle)
    language = _first_text(
        _nested(bundle, "caption_language", "language"),
        _get(profile, "language"),
    ) or None
    word_timings = _get(
        voice, "word_timings", "alignment_words", "words",
        "word_timestamps", "boundaries", "alignment",
    )
    sub_result = None
    if word_timings:
        sub_result = build_subtitles(
            word_timings,
            script_text=script_text or None,
            script_sentences=script_sentences or None,
            profile=captions_profile,
            language=language,
        )
    else:
        transcription_audio = _resolve_transcription_audio(voice, audio_path)
        notes["subtitle_audio_source"] = (
            "narration" if transcription_audio != audio_path else "master"
        )
        segments = _transcribe_for_subtitles(
            transcription_audio,
            language=_whisper_language(language, captions_profile),
            script_text=script_text,
            duration_s=duration_s,
            notes=notes,
        )
        if segments:
            sub_result = build_subtitles(
                transcription_segments=segments,
                script_text=script_text or None,
                script_sentences=script_sentences or None,
                profile=captions_profile,
                language=language,
            )
    if sub_result is None:
        # Last resort: scene sentences spread over the measured duration.
        scene_text = " ".join(
            it["source_sentence"] for it in items if it["source_sentence"]
        ).strip()
        fallback_text = scene_text or script_text
        if fallback_text and duration_s > 0:
            from app.pipeline.subtitles.whisper_wrapper import (
                segments_from_script_duration,
            )

            sub_result = build_subtitles(
                transcription_segments=segments_from_script_duration(
                    fallback_text, duration_s),
                script_text=fallback_text,
                profile=captions_profile,
                language=language,
            )
    if sub_result is not None:
        captions_srt = sub_result.srt_text
        subtitle_source = sub_result.source
        subtitle_warnings = list(sub_result.warnings)
        write_srt_sidecar(sub_result, pack_dir / "captions.srt")
    else:
        captions_srt = ""
        subtitle_source = SOURCE_ALIGNMENT_STUB
        subtitle_warnings = ["no script, timings or scene text: empty captions"]
        (pack_dir / "captions.srt").write_text("", encoding="utf-8")
    subtitle_path_used = (
        "alignment-stub" if subtitle_source == SOURCE_ALIGNMENT_STUB
        else subtitle_source
    )
    # R4-005: the production QC report (this provenance dict is embedded
    # verbatim as qc_summary["subtitles"] below) carries BOTH subtitle
    # script-match scores — the strict script_match_ratio and the
    # transliteration-aware script_match_ratio_fair — plus
    # script_match_metrics, which pairs each score with its metric
    # name.  The fair score is reporting/WARN context only; no gate
    # threshold reads it (SUBTITLE_SCRIPT_MATCH_GATE_POLICY).
    notes["subtitle_provenance"] = {
        "source": subtitle_source,
        "path_used": subtitle_path_used,
        "cue_count": sub_result.cue_count if sub_result else 0,
        "script_match_ratio": (
            sub_result.script_match_ratio if sub_result else None),
        "script_match_ratio_fair": (
            sub_result.script_match_ratio_fair if sub_result else None),
        "script_match_metrics": (
            sub_result.script_match_metrics if sub_result else None),
    }
    # Additive Round-3 detail: which whisper model was attempted, what
    # the transcriber itself reported, and why the stub ran (if it did).
    for _src_key, _dst_key in (
        ("subtitle_whisper_model", "whisper_model"),
        ("subtitle_transcriber_provenance", "transcriber_provenance"),
        ("subtitle_whisper_error", "whisper_error"),
        ("subtitle_audio_source", "audio_source"),
    ):
        if notes.get(_src_key):
            notes["subtitle_provenance"][_dst_key] = notes[_src_key]

    # -- thumbnails (3 variants; hero plate = first still / video frame) -----
    title = _first_text(
        _nested(bundle, "title", "video_title"),
        _get(bundle, "topic"),
    )
    thumb_text_raw = _nested(
        bundle, "thumbnail_text", "thumbnail_headline", "headline", "thumb_text")
    headlines: list[str] = []
    for candidate in _as_str_list(thumb_text_raw) + _as_str_list(
        _nested(bundle, "thumbnail_headlines", "headlines")
    ):
        if candidate not in headlines:
            headlines.append(candidate)
    if title and title not in headlines:
        headlines.append(title)
    short_title = " ".join(title.split()[:4]).strip()
    if short_title and short_title not in headlines:
        headlines.append(short_title)
    if not headlines:
        headlines = ["Watch This"]

    backgrounds: list[Any] = [
        it["path"] for it in items
        if it["path"] and Path(it["path"]).suffix.lower() in _IMAGE_SUFFIXES
        and Path(it["path"]).is_file()
    ]
    if not backgrounds:
        frame_path = work_dir / "hero_frame.png"
        try:
            import subprocess

            proc = subprocess.run(
                [ffmpeg_executable(), "-y", "-v", "error", "-ss", "1", "-i",
                 str(final_mp4), "-frames:v", "1", str(frame_path)],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode == 0 and frame_path.is_file():
                backgrounds = [str(frame_path)]
        except Exception as exc:
            notes["hero_frame_error"] = f"{type(exc).__name__}: {exc}"
    if not backgrounds:
        from PIL import Image

        plate = work_dir / "hero_plate.png"
        Image.new("RGB", (1280, 720), (16, 20, 24)).save(str(plate))
        backgrounds = [str(plate)]
        notes["thumbnail_background"] = "solid plate (no stills available)"

    thumb_profile = _thumbnail_profile(profile)
    # -- Style Lab: a resolved style profile's thumbnail directives
    #    take precedence over the niche-profile default above (which
    #    remains the fallback); the headline is band-checked against
    #    the profile's text-word band.  No profile -> unchanged.
    style_applied: dict[str, Any] = {}
    thumb_directives = _resolve_style_thumbnail_directives(
        ctx, bundle, pack_dir, work_dir)
    if thumb_directives is not None:
        mapping = apply_style_thumbnail(thumb_profile, thumb_directives)
        thumb_profile = mapping.profile
        headlines, adjustment = adjust_headlines(
            headlines, mapping.text_words_min, mapping.text_words_max)
        style_applied = {
            "thumbnail": {
                "layout_class": mapping.layout_class,
                "text_words_band": [
                    mapping.text_words_min, mapping.text_words_max],
                "headline_words_before": (
                    adjustment.words_before if adjustment else None),
                "headline_words_after": (
                    adjustment.words_after if adjustment else None),
                "headline_action": (
                    adjustment.action if adjustment else None),
                "face_expected": mapping.face_expected,
                "palette_class": mapping.palette_class,
                "unmapped": list(mapping.unmapped),
            }
        }
        notes["style_applied"] = style_applied
    variant_set = generate_variants(
        backgrounds, headlines, thumb_profile,
        work_dir / "thumbnails", base_name="thumbnail",
    )
    thumbnails: list[str] = []
    for i, variant in enumerate(variant_set.variants, start=1):
        dest = pack_dir / f"thumbnail_{i}.png"
        shutil.copy2(variant.path, dest)
        thumbnails.append(str(dest))
        notes.setdefault("thumbnail_variants", []).append(
            {
                "file": dest.name,
                "layout": variant.layout_id,
                "headline": variant.headline,
                "legibility_passed": (
                    variant.legibility.passed if variant.legibility else None),
                "warnings": list(variant.warnings),
            }
        )

    # -- technical QC on the final MP4 (module verdicts taken verbatim) ------
    qc_report = run_technical_qc(
        final_mp4,
        contract=MediaContract(
            expected_duration_s=duration_s,
            duration_tolerance_s=0.6,
            # The expected resolution is the tier actually rendered
            # (1280x720 for draft — unchanged); every QC threshold in
            # the QC module itself is exactly as before.
            expected_width=tier_spec.width,
            expected_height=tier_spec.height,
            expected_fps=float(FPS),
        ),
        declared_still_ranges=declared_still_ranges,
    )
    loudness_lufs = (
        qc_report.loudness.integrated_lufs if qc_report.loudness else None
    )
    qc_summary: dict[str, Any] = {
        "verdict": qc_report.verdict.value,
        "passed": qc_report.verdict is Verdict.PASS,
        "media_path": str(final_mp4),
        "duration_s": duration_s,
        "checks": {
            c.check_id: {
                "verdict": c.verdict.value,
                "reason_codes": list(c.reason_codes),
            }
            for c in qc_report.checks
        },
        "loudness": (
            {
                "integrated_lufs": qc_report.loudness.integrated_lufs,
                "true_peak_dbtp": qc_report.loudness.true_peak_dbtp,
                "lra_lu": qc_report.loudness.lra_lu,
                "source": qc_report.loudness.source,
            }
            if qc_report.loudness else None
        ),
        "detections": {
            name: len(ranges) for name, ranges in qc_report.detections.items()
        },
        "subtitles": notes.get("subtitle_provenance", {}),
        # Round 5 render record — carried into report.md via the pack
        # writer's QC-summary rendering (produce.py unchanged).
        "quality_tier": resolved_tier,
        "render_wall_time_s": render_wall_time_s,
        "output_file_size_bytes": output_file_size_bytes,
    }

    return AssembleResult(
        final_mp4=str(final_mp4),
        duration_s=float(duration_s),
        captions_srt=captions_srt,
        subtitle_path_used=subtitle_path_used,
        thumbnails=thumbnails,
        qc_passed=qc_report.verdict is Verdict.PASS,
        qc_summary=qc_summary,
        loudness_lufs=loudness_lufs,
        render_path_used=resolved_tier,
        quality_tier=resolved_tier,
        render_wall_time_s=render_wall_time_s,
        output_file_size_bytes=output_file_size_bytes,
        subtitle_source=subtitle_source,
        subtitle_warnings=subtitle_warnings,
        segment_paths=[str(p) for p in segment_paths],
        licence_segments=licence_segments,
        pack_dir=str(pack_dir),
        work_dir=str(work_dir),
        qc_report=qc_report,
        notes=notes,
        style_applied=style_applied,
    )
