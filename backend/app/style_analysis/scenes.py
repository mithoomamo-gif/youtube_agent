"""Style Lab — scene segmentation & editing metrics for a reference video.

Given one competitor/reference video file, this module measures *how it
is edited*, in the shared Style Lab field names:

* ``scenes`` — list of ``{index, start_s, end_s, duration_s}``
* aggregates — ``{scene_count, avg_shot_length_s, cuts_per_minute}``
* motion — ``{zoom_events, pan_events, motion_level}``

Method (FFmpeg only, no new dependencies)
------------------------------------------
Scene boundaries come from FFmpeg's built-in scene-change score:
``select='gt(scene,THRESHOLD)'`` + ``showinfo`` on a stream downscaled
to :data:`ANALYSIS_WIDTH` px wide.  The score is a per-frame 0..1
measure of how much a frame differs from its predecessor.

Threshold: :data:`DEFAULT_SCENE_THRESHOLD` = **0.3**.
Rationale, measured on this project's own Phase-0 facts video
(185.7 s of same-theme ocean stock, assembled from 25 stock clips):
true hard cuts scored 0.18-0.84 — cuts between *distinct* content
score 0.5+, but cuts between visually similar same-theme stock shots
score as low as ~0.2-0.4 because the palette barely changes.  The
commonly cited FFmpeg example value 0.4 found only 15 of ~25 scenes
there; 0.3 found 21, and every frame scoring >= 0.15 sat at a
plausible edit point (resulting shot lengths 2.6-15.4 s).  0.3 is
therefore the domain default for faceless stock montages; it stays
above the < ~0.2 scores produced by in-shot camera motion / Ken Burns
zooms on continuous shots.  ``threshold`` is a parameter on every
entry point: raise it towards 0.4-0.5 for mixed-content videos where
false splits on pans are the bigger risk, lower it towards 0.2 for
single-theme montages where missed cuts are.

Hard cuts vs gradual transitions — honest limits
-------------------------------------------------
The scene score is *frame-to-frame*.  A hard cut concentrates all change
in one frame pair and is detected reliably, timed to the exact frame.
A gradual transition (dissolve / cross-fade / fade through black spread
over ~0.3-1 s) spreads the same change over many frame pairs, so each
pair can stay below threshold: gradual transitions may be **missed**,
or detected late at reduced precision.  This module never papers over
that: results carry a ``notes`` entry saying so, and a video where
almost nothing is detected is flagged ``low_segmentation`` instead of
being presented as confidently single-scene (see fallback below).

Fallback (no invented cuts)
----------------------------
If detection yields implausibly few scenes for the duration — a single
scene spanning a long video, or an average shot length over
:data:`LOW_SEGMENTATION_MAX_ASL_S` on a long video — the result is one
scene per detected span (in the extreme, one scene spanning the whole
video) with ``low_segmentation=True``.  We do **not** synthesise cuts
to make the numbers look edited: ``low_segmentation`` means "this
looks like a single take, or its transitions are gradual/below
threshold — segmentation confidence is low".

Motion estimate — what is and is not measured
----------------------------------------------
Motion *level* is measured, zoom/pan are **not** estimated.  Sampled
frames (``fps=1``, downscaled to :data:`MOTION_WIDTH` px, grayscale)
are differenced with ``tblend=all_mode=difference`` and the mean luma
of the difference (``signalstats`` YAVG) is the mean absolute frame
difference for that sample interval.  Both the mean and the median of
those per-interval differences are reported
(``mean_abs_frame_diff`` / ``median_abs_frame_diff``); the *level* is
bucketed from the **median** by :func:`classify_motion_level`, because
hard cuts inject a few extreme differences (50-100+) that dominate a
mean and would grade a calm but frequently-cut video as "high
motion" — on the Phase-0 facts video the mean is 10.0 while the
median is 2.9.  The level is a within-shot motion signal; cut density
is already carried by ``cuts_per_minute``.  Estimating zoom/pan
counts reliably would need
feature tracking / phase correlation across frames; sampled-frame drift
heuristics fabricate plausible-looking numbers, so ``zoom_events`` and
``pan_events`` are the literal string ``"not_estimated"`` rather than
invented counts.

Performance discipline
-----------------------
Nothing is decoded at full resolution: the scene pass analyses a
:data:`ANALYSIS_WIDTH` px-wide stream and the motion pass a
:data:`MOTION_WIDTH` px-wide stream sampled at 1 fps, so cost scales
with duration, not pixels.  Both passes are single FFmpeg invocations.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from pathlib import Path
from typing import Literal, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..ffmpeg_resolution import ffmpeg_executable, ffprobe_executable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default FFmpeg scene-change threshold (see module docstring for
#: the measured rationale: 0.3, not the commonly cited 0.4).
DEFAULT_SCENE_THRESHOLD = 0.3

#: Width (px) the scene-detection pass downscales to before scoring.
ANALYSIS_WIDTH = 320

#: Width (px) the motion pass downscales to before differencing.
MOTION_WIDTH = 160

#: Motion sampling rate (frames per second) for the differencing pass.
MOTION_SAMPLE_FPS = 1.0

#: Detected scenes shorter than this are merged into their neighbour.
#: Filters single-frame flashes / double-detections inside one gradual
#: transition without moving real cut points by more than this much.
MIN_SCENE_DURATION_S = 0.5

#: A lone scene at least this long is implausible as edited content.
LOW_SEGMENTATION_MIN_DURATION_S = 20.0

#: On videos at least this long, an average shot length above
#: LOW_SEGMENTATION_MAX_ASL_S also marks low segmentation.
LOW_SEGMENTATION_LONG_VIDEO_S = 60.0
LOW_SEGMENTATION_MAX_ASL_S = 30.0

#: Median-absolute-frame-difference bands (0-255 luma, 160 px gray,
#: 1 s sample interval) for the motion level.  Heuristic, anchored on
#: measured references: a static shot ~0; slow stock (drifting ocean,
#: slow pans) median ~3; busy synthetic motion (testsrc2) median ~6+.
MOTION_LOW_MAX = 1.5
MOTION_MODERATE_MAX = 6.0

FFMPEG_TIMEOUT_S = 900.0
FFPROBE_TIMEOUT_S = 30.0

_NOT_ESTIMATED = "not_estimated"

_GRADUAL_TRANSITION_NOTE = (
    "Scene scores are frame-to-frame: hard cuts are detected reliably, "
    "but gradual transitions (dissolves/fades spread over many frames) "
    "may be missed or timed imprecisely."
)
_ZOOM_PAN_NOTE = (
    "zoom_events/pan_events are 'not_estimated': sampled-frame drift "
    "cannot separate camera zoom/pan from subject motion reliably, so "
    "no counts are fabricated; motion_level is the measured signal."
)


class SceneAnalysisError(RuntimeError):
    """FFmpeg/ffprobe could not produce a trustworthy measurement."""


# ---------------------------------------------------------------------------
# Models (shared Style Lab field names — do not rename)
# ---------------------------------------------------------------------------


class Scene(BaseModel):
    """One detected scene (shot) on the video timeline."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    duration_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_span(self) -> "Scene":
        if self.end_s <= self.start_s:
            raise ValueError("scene end_s must be after start_s")
        return self


class EditingAggregates(BaseModel):
    """Editing-rhythm aggregates over a scene list."""

    model_config = ConfigDict(extra="forbid")

    scene_count: int = Field(ge=0)
    #: Mean scene duration (duration / scene_count); 0.0 when no scenes.
    avg_shot_length_s: float = Field(ge=0)
    #: (scene_count - 1) transitions per minute of video; 0.0 for a
    #: single scene (a lone take has no cuts, however long it is).
    cuts_per_minute: float = Field(ge=0)


class MotionEstimate(BaseModel):
    """Video-level motion estimate.

    ``zoom_events`` / ``pan_events`` are counts when a future estimator
    can measure them and the literal ``"not_estimated"`` until then —
    never a guessed number.
    """

    model_config = ConfigDict(extra="forbid")

    zoom_events: Union[int, Literal["not_estimated"]] = _NOT_ESTIMATED
    pan_events: Union[int, Literal["not_estimated"]] = _NOT_ESTIMATED
    motion_level: Literal["low", "moderate", "high", "unknown"] = "unknown"
    #: Mean absolute frame difference (0-255 luma) over sampled frame
    #: pairs.  Reported verbatim, but NOT used for ``motion_level``:
    #: hard-cut intervals inflate it (see module docstring).  None
    #: when no pair could be sampled (e.g. a sub-second video).
    mean_abs_frame_diff: Optional[float] = None
    #: Median absolute frame difference over sampled pairs — the
    #: robust statistic ``motion_level`` is bucketed from.
    median_abs_frame_diff: Optional[float] = None
    #: Number of sampled frames the estimate is built from.
    sampled_frames: int = Field(default=0, ge=0)


class SceneSegmentation(BaseModel):
    """Scene-detection result, including honesty flags."""

    model_config = ConfigDict(extra="forbid")

    scenes: list[Scene]
    duration_s: float = Field(gt=0)
    threshold: float
    #: True when implausibly few scenes were found for the duration
    #: (single take, or transitions below threshold) — segmentation
    #: confidence is low and no cuts were invented to fill the gap.
    low_segmentation: bool = False
    notes: list[str] = Field(default_factory=list)


class SceneAnalysis(BaseModel):
    """Full Style Lab editing analysis of one reference video."""

    model_config = ConfigDict(extra="forbid")

    scenes: list[Scene]
    aggregates: EditingAggregates
    motion: MotionEstimate
    duration_s: float = Field(gt=0)
    threshold: float
    low_segmentation: bool = False
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Probing & FFmpeg plumbing
# ---------------------------------------------------------------------------


def probe_duration_s(video_path: Union[str, Path]) -> float:
    """Return the container duration (seconds) of ``video_path``.

    Mirrors the pipeline timing probe (ffprobe ``format=duration``);
    implemented locally so Style Lab stays self-contained.  Raises
    :class:`SceneAnalysisError` on any failure — never guesses.
    """
    path = Path(video_path)
    if not path.is_file():
        raise SceneAnalysisError(f"video file does not exist: {path}")
    cmd = [
        ffprobe_executable(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = _run(cmd, FFPROBE_TIMEOUT_S, "ffprobe")
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SceneAnalysisError(
            f"ffprobe returned no usable duration for {path}"
        ) from exc
    if duration <= 0:
        raise SceneAnalysisError(f"ffprobe reported non-positive duration for {path}")
    return duration


def _run(cmd: list[str], timeout_s: float, tool: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s
        )
    except FileNotFoundError as exc:
        raise SceneAnalysisError(f"{tool} binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SceneAnalysisError(f"{tool} timed out after {timeout_s:.0f}s") from exc
    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-300:]
        raise SceneAnalysisError(f"{tool} failed ({cmd[0]}): {tail}")
    return result


# ---------------------------------------------------------------------------
# Scene detection
# ---------------------------------------------------------------------------

_SHOWINFO_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def parse_showinfo_cut_times(stderr_text: str) -> list[float]:
    """Extract cut timestamps from FFmpeg ``showinfo`` stderr output.

    Pure helper (unit-testable without FFmpeg).  Only lines emitted by
    the showinfo filter are considered; the filter's own ``config``
    lines carry no ``pts_time`` and are ignored naturally.
    """
    times: list[float] = []
    for line in stderr_text.splitlines():
        if "showinfo" not in line:
            continue
        match = _SHOWINFO_PTS_RE.search(line)
        if match:
            times.append(float(match.group(1)))
    return sorted(times)


def build_scenes(
    cut_times: Sequence[float],
    duration_s: float,
    *,
    min_scene_duration_s: float = MIN_SCENE_DURATION_S,
) -> list[Scene]:
    """Build contiguous scenes from sorted cut times + total duration.

    Cuts closer than ``min_scene_duration_s`` to the previous kept
    boundary — or so close to the end that the tail scene would be
    shorter than the minimum — are dropped (merged), so one gradual
    transition cannot shatter into slivers.  With no usable cuts the
    result is exactly one scene spanning the whole video.
    """
    if duration_s <= 0:
        raise SceneAnalysisError("duration_s must be positive to build scenes")
    kept: list[float] = [0.0]
    for cut in sorted(cut_times):
        if cut <= 0.0 or cut >= duration_s:
            continue
        if cut - kept[-1] < min_scene_duration_s:
            continue  # too soon after previous boundary: same transition
        if duration_s - cut < min_scene_duration_s:
            continue  # tail would be a sliver: merge into last scene
        kept.append(cut)
    boundaries = kept + [duration_s]
    scenes: list[Scene] = []
    for index in range(len(boundaries) - 1):
        start = round(boundaries[index], 3)
        end = round(boundaries[index + 1], 3)
        if end <= start:  # rounding guard; cannot happen post-merge
            continue
        scenes.append(
            Scene(
                index=len(scenes),
                start_s=start,
                end_s=end,
                duration_s=round(end - start, 3),
            )
        )
    return scenes


def is_low_segmentation(scene_count: int, duration_s: float) -> bool:
    """True when the scene count is implausibly low for the duration."""
    if scene_count <= 1:
        return duration_s >= LOW_SEGMENTATION_MIN_DURATION_S
    if duration_s >= LOW_SEGMENTATION_LONG_VIDEO_S:
        return (duration_s / scene_count) > LOW_SEGMENTATION_MAX_ASL_S
    return False


def segment_video(
    video_path: Union[str, Path],
    *,
    threshold: float = DEFAULT_SCENE_THRESHOLD,
    min_scene_duration_s: float = MIN_SCENE_DURATION_S,
    timeout_s: float = FFMPEG_TIMEOUT_S,
) -> SceneSegmentation:
    """Detect scenes in ``video_path`` (rich result; see module docs).

    Runs one FFmpeg pass over a downscaled stream:
    ``scale=320:-2,select='gt(scene,THRESHOLD)',showinfo``.
    """
    if not 0.0 < threshold < 1.0:
        raise SceneAnalysisError(f"threshold must be in (0, 1), got {threshold}")
    path = Path(video_path)
    duration_s = probe_duration_s(path)
    vf = (
        f"scale={ANALYSIS_WIDTH}:-2,"
        f"select='gt(scene,{threshold})',showinfo"
    )
    cmd = [
        ffmpeg_executable(),
        "-hide_banner",
        "-i",
        str(path),
        "-an",
        "-vf",
        vf,
        "-f",
        "null",
        "-",
    ]
    result = _run(cmd, timeout_s, "ffmpeg")
    cut_times = parse_showinfo_cut_times(result.stderr or "")
    scenes = build_scenes(
        cut_times, duration_s, min_scene_duration_s=min_scene_duration_s
    )
    low = is_low_segmentation(len(scenes), duration_s)
    notes = [f"Scene threshold {threshold} (FFmpeg gt(scene,t) score)."]
    notes.append(_GRADUAL_TRANSITION_NOTE)
    if low:
        notes.append(
            "Low segmentation: implausibly few scenes for the duration — "
            "the video is a single take or its transitions are gradual / "
            "below threshold. No cuts were invented."
        )
    elif not cut_times:
        notes.append("No scene changes above threshold were detected.")
    return SceneSegmentation(
        scenes=scenes,
        duration_s=duration_s,
        threshold=threshold,
        low_segmentation=low,
        notes=notes,
    )


def detect_scenes(
    video_path: Union[str, Path],
    *,
    threshold: float = DEFAULT_SCENE_THRESHOLD,
    min_scene_duration_s: float = MIN_SCENE_DURATION_S,
    timeout_s: float = FFMPEG_TIMEOUT_S,
) -> list[Scene]:
    """Return the detected scenes of ``video_path`` as ``list[Scene]``.

    Thin wrapper over :func:`segment_video`; callers that need the
    ``low_segmentation`` flag / notes should use that (or
    :func:`analyse_video`) instead.  When detection finds implausibly
    few scenes the fallback is a single scene spanning the whole
    video — cuts are never invented.
    """
    return segment_video(
        video_path,
        threshold=threshold,
        min_scene_duration_s=min_scene_duration_s,
        timeout_s=timeout_s,
    ).scenes


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


def compute_aggregates(
    scenes: Sequence[Scene],
    duration_s: Optional[float] = None,
) -> EditingAggregates:
    """Compute ``scene_count`` / ASL / cuts-per-minute for ``scenes``.

    ``duration_s`` defaults to the last scene's ``end_s`` (the scenes
    built by :func:`build_scenes` are contiguous from 0).  An empty
    scene list yields all-zero aggregates rather than an error, so
    reporting code can render it honestly.
    """
    scene_count = len(scenes)
    if scene_count == 0:
        return EditingAggregates(
            scene_count=0, avg_shot_length_s=0.0, cuts_per_minute=0.0
        )
    total = duration_s if duration_s is not None else scenes[-1].end_s
    if total <= 0:
        raise SceneAnalysisError("duration must be positive to aggregate scenes")
    avg_shot_length = total / scene_count
    cuts = scene_count - 1
    cuts_per_minute = (cuts / total) * 60.0 if cuts > 0 else 0.0
    return EditingAggregates(
        scene_count=scene_count,
        avg_shot_length_s=round(avg_shot_length, 3),
        cuts_per_minute=round(cuts_per_minute, 3),
    )


# ---------------------------------------------------------------------------
# Motion estimate
# ---------------------------------------------------------------------------

_YAVG_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9]+(?:\.[0-9]+)?)")


def parse_frame_diff_values(stderr_text: str) -> list[float]:
    """Extract per-sample mean-absolute-difference values (pure helper).

    Parses ``lavfi.signalstats.YAVG`` lines printed by the
    ``metadata=print`` filter from the differenced stream.
    """
    return [float(m.group(1)) for m in _YAVG_RE.finditer(stderr_text)]


def classify_motion_level(median_abs_frame_diff: Optional[float]) -> str:
    """Bucket a median absolute frame difference into a motion level."""
    if median_abs_frame_diff is None:
        return "unknown"
    if median_abs_frame_diff < MOTION_LOW_MAX:
        return "low"
    if median_abs_frame_diff < MOTION_MODERATE_MAX:
        return "moderate"
    return "high"


def estimate_motion(
    video_path: Union[str, Path],
    *,
    sample_fps: float = MOTION_SAMPLE_FPS,
    timeout_s: float = FFMPEG_TIMEOUT_S,
) -> MotionEstimate:
    """Estimate the video-level motion level of ``video_path``.

    One FFmpeg pass: sample at ``sample_fps``, downscale to
    :data:`MOTION_WIDTH` px grayscale, difference consecutive samples
    (``tblend``), read the difference mean (``signalstats`` YAVG).
    ``zoom_events`` / ``pan_events`` stay ``"not_estimated"`` — see the
    module docstring for why no counts are fabricated.
    """
    path = Path(video_path)
    if not path.is_file():
        raise SceneAnalysisError(f"video file does not exist: {path}")
    vf = (
        f"fps={sample_fps},scale={MOTION_WIDTH}:-2,format=gray,"
        "tblend=all_mode=difference,signalstats,"
        "metadata=print:key=lavfi.signalstats.YAVG"
    )
    cmd = [
        ffmpeg_executable(),
        "-hide_banner",
        "-i",
        str(path),
        "-an",
        "-vf",
        vf,
        "-f",
        "null",
        "-",
    ]
    result = _run(cmd, timeout_s, "ffmpeg")
    diffs = parse_frame_diff_values(result.stderr or "")
    if not diffs:
        return MotionEstimate(
            zoom_events=_NOT_ESTIMATED,
            pan_events=_NOT_ESTIMATED,
            motion_level="unknown",
            mean_abs_frame_diff=None,
            median_abs_frame_diff=None,
            sampled_frames=0,
        )
    mean_diff = sum(diffs) / len(diffs)
    median_diff = statistics.median(diffs)
    return MotionEstimate(
        zoom_events=_NOT_ESTIMATED,
        pan_events=_NOT_ESTIMATED,
        motion_level=classify_motion_level(median_diff),  # type: ignore[arg-type]
        mean_abs_frame_diff=round(mean_diff, 4),
        median_abs_frame_diff=round(median_diff, 4),
        # tblend emits one difference frame per sampled pair.
        sampled_frames=len(diffs) + 1,
    )


# ---------------------------------------------------------------------------
# Full analysis
# ---------------------------------------------------------------------------


def analyse_video(
    video_path: Union[str, Path],
    *,
    threshold: float = DEFAULT_SCENE_THRESHOLD,
    include_motion: bool = True,
    timeout_s: float = FFMPEG_TIMEOUT_S,
) -> SceneAnalysis:
    """Full editing analysis: scenes + aggregates + motion estimate."""
    segmentation = segment_video(
        video_path, threshold=threshold, timeout_s=timeout_s
    )
    aggregates = compute_aggregates(
        segmentation.scenes, duration_s=segmentation.duration_s
    )
    if include_motion:
        motion = estimate_motion(video_path, timeout_s=timeout_s)
    else:
        motion = MotionEstimate()
    notes = list(segmentation.notes)
    notes.append(_ZOOM_PAN_NOTE)
    return SceneAnalysis(
        scenes=segmentation.scenes,
        aggregates=aggregates,
        motion=motion,
        duration_s=segmentation.duration_s,
        threshold=segmentation.threshold,
        low_segmentation=segmentation.low_segmentation,
        notes=notes,
    )


#: US-spelling alias.
analyze_video = analyse_video

__all__ = [
    "ANALYSIS_WIDTH",
    "DEFAULT_SCENE_THRESHOLD",
    "MIN_SCENE_DURATION_S",
    "MOTION_WIDTH",
    "EditingAggregates",
    "MotionEstimate",
    "Scene",
    "SceneAnalysis",
    "SceneAnalysisError",
    "SceneSegmentation",
    "analyse_video",
    "analyze_video",
    "build_scenes",
    "classify_motion_level",
    "compute_aggregates",
    "detect_scenes",
    "estimate_motion",
    "is_low_segmentation",
    "parse_frame_diff_values",
    "parse_showinfo_cut_times",
    "probe_duration_s",
    "segment_video",
]
