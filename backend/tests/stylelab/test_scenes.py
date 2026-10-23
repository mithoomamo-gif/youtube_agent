"""Tests for app.style_analysis.scenes (scene segmentation & editing metrics).

Two layers:

* Pure unit tests for the parsing / scene-building / aggregate /
  classification helpers — no FFmpeg needed, always run.
* FFmpeg integration tests against synthetic videos generated in-test
  with known ground truth (a 4-segment colour concat has exactly 3
  hard cuts; a single static colour take has none).  Skipped when
  ffmpeg/ffprobe are not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.style_analysis.scenes import (
    MotionEstimate,
    Scene,
    SceneAnalysisError,
    analyse_video,
    build_scenes,
    classify_motion_level,
    compute_aggregates,
    detect_scenes,
    estimate_motion,
    is_low_segmentation,
    parse_frame_diff_values,
    parse_showinfo_cut_times,
    segment_video,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
ffmpeg_required = pytest.mark.skipif(
    not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH"
)


def _run_ffmpeg(args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _make_colour_concat(path: Path) -> Path:
    """4 solid-colour segments x 2 s -> hard cuts at exactly 2/4/6 s."""
    inputs: list[str] = []
    for colour in ("red", "blue", "green", "yellow"):
        inputs += ["-f", "lavfi", "-i", f"color=c={colour}:s=320x240:r=10:d=2"]
    _run_ffmpeg(
        [
            *inputs,
            "-filter_complex",
            "[0][1][2][3]concat=n=4:v=1:a=0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
            "-y",
        ]
    )
    return path


def _make_static(path: Path, *, duration_s: int = 25) -> Path:
    """One static colour for the whole duration: a true single take."""
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=navy:s=320x240:r=5:d={duration_s}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
            "-y",
        ]
    )
    return path


def _make_moving(path: Path) -> Path:
    """testsrc2: continuous synthetic motion, no cuts."""
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x240:r=10:d=6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
            "-y",
        ]
    )
    return path


@pytest.fixture(scope="module")
def colour_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_colour_concat(tmp_path_factory.mktemp("sl") / "colour.mp4")


@pytest.fixture(scope="module")
def static_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_static(tmp_path_factory.mktemp("sl") / "static.mp4")


@pytest.fixture(scope="module")
def moving_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_moving(tmp_path_factory.mktemp("sl") / "moving.mp4")


# ---------------------------------------------------------------------------
# Pure helpers: showinfo parsing
# ---------------------------------------------------------------------------

_SHOWINFO_SAMPLE = """\
[Parsed_showinfo_2 @ 0x1] config in time_base: 1/10240, frame_rate: 10/1
[Parsed_showinfo_2 @ 0x1] n:   0 pts:  20480 pts_time:2       duration: 1024 fmt:yuv420p
[Parsed_showinfo_2 @ 0x1] n:   1 pts:  40960 pts_time:4.5     duration: 1024 fmt:yuv420p
Input #0, mov,mp4, from 'x.mp4':
frame=    3 fps=10 q=-0.0 time=00:00:04.50
"""


def test_parse_showinfo_cut_times() -> None:
    assert parse_showinfo_cut_times(_SHOWINFO_SAMPLE) == [2.0, 4.5]


def test_parse_showinfo_empty() -> None:
    assert parse_showinfo_cut_times("") == []
    assert parse_showinfo_cut_times("no relevant lines\n") == []


def test_parse_frame_diff_values() -> None:
    text = (
        "[metadata] lavfi.signalstats.YAVG=0\n"
        "[metadata] lavfi.signalstats.YAVG=46.0333\n"
        "[metadata] lavfi.signalstats.YMIN=0\n"
    )
    assert parse_frame_diff_values(text) == [0.0, 46.0333]
    assert parse_frame_diff_values("") == []


# ---------------------------------------------------------------------------
# Pure helpers: scene building
# ---------------------------------------------------------------------------


def test_build_scenes_basic() -> None:
    scenes = build_scenes([2.0, 4.0, 6.0], 8.0)
    assert [(s.index, s.start_s, s.end_s, s.duration_s) for s in scenes] == [
        (0, 0.0, 2.0, 2.0),
        (1, 2.0, 4.0, 2.0),
        (2, 4.0, 6.0, 2.0),
        (3, 6.0, 8.0, 2.0),
    ]


def test_build_scenes_no_cuts_single_span() -> None:
    scenes = build_scenes([], 12.5)
    assert len(scenes) == 1
    assert scenes[0].start_s == 0.0
    assert scenes[0].end_s == 12.5
    assert scenes[0].duration_s == 12.5


def test_build_scenes_merges_close_cuts() -> None:
    # 2.2 is within MIN_SCENE_DURATION (0.5) of 2.0: same transition.
    scenes = build_scenes([2.0, 2.2, 5.0], 8.0)
    assert [s.start_s for s in scenes] == [0.0, 2.0, 5.0]


def test_build_scenes_drops_sliver_tail() -> None:
    # A cut 0.2 s before the end would leave a sliver scene: merged.
    scenes = build_scenes([3.0, 7.8], 8.0)
    assert [s.start_s for s in scenes] == [0.0, 3.0]
    assert scenes[-1].end_s == 8.0


def test_build_scenes_ignores_out_of_range_cuts() -> None:
    scenes = build_scenes([-1.0, 0.0, 8.0, 99.0, 4.0], 8.0)
    assert [s.start_s for s in scenes] == [0.0, 4.0]


def test_build_scenes_rejects_nonpositive_duration() -> None:
    with pytest.raises(SceneAnalysisError):
        build_scenes([], 0.0)


# ---------------------------------------------------------------------------
# Pure helpers: aggregates / low-segmentation / motion bands
# ---------------------------------------------------------------------------


def _scenes(*durations: float) -> list[Scene]:
    out: list[Scene] = []
    start = 0.0
    for index, dur in enumerate(durations):
        out.append(
            Scene(index=index, start_s=start, end_s=start + dur, duration_s=dur)
        )
        start += dur
    return out


def test_compute_aggregates_math() -> None:
    aggs = compute_aggregates(_scenes(2.0, 2.0, 2.0, 2.0))
    assert aggs.scene_count == 4
    assert aggs.avg_shot_length_s == 2.0
    assert aggs.cuts_per_minute == 22.5  # 3 cuts / 8 s


def test_compute_aggregates_single_scene_has_no_cuts() -> None:
    aggs = compute_aggregates(_scenes(90.0))
    assert aggs.scene_count == 1
    assert aggs.avg_shot_length_s == 90.0
    assert aggs.cuts_per_minute == 0.0


def test_compute_aggregates_empty() -> None:
    aggs = compute_aggregates([])
    assert aggs.scene_count == 0
    assert aggs.avg_shot_length_s == 0.0
    assert aggs.cuts_per_minute == 0.0


def test_compute_aggregates_explicit_duration() -> None:
    aggs = compute_aggregates(_scenes(5.0, 5.0), duration_s=20.0)
    assert aggs.avg_shot_length_s == 10.0
    assert aggs.cuts_per_minute == 3.0


def test_is_low_segmentation_rules() -> None:
    assert is_low_segmentation(1, 25.0) is True  # long single take
    assert is_low_segmentation(1, 8.0) is False  # short clip: fine
    assert is_low_segmentation(4, 8.0) is False
    assert is_low_segmentation(2, 120.0) is True  # ASL 60 s on long video
    assert is_low_segmentation(10, 120.0) is False  # ASL 12 s: normal


def test_classify_motion_level_bands() -> None:
    assert classify_motion_level(None) == "unknown"
    assert classify_motion_level(0.0) == "low"
    assert classify_motion_level(1.4) == "low"
    assert classify_motion_level(3.0) == "moderate"
    assert classify_motion_level(12.0) == "high"


# ---------------------------------------------------------------------------
# Shared-schema field names (contract with the Style Lab schema worker)
# ---------------------------------------------------------------------------


def test_shared_schema_field_names() -> None:
    scene = Scene(index=0, start_s=0.0, end_s=2.0, duration_s=2.0)
    assert set(scene.model_dump()) == {"index", "start_s", "end_s", "duration_s"}

    aggs = compute_aggregates([scene])
    assert set(aggs.model_dump()) == {
        "scene_count",
        "avg_shot_length_s",
        "cuts_per_minute",
    }

    motion = MotionEstimate()
    dumped = motion.model_dump()
    assert {"zoom_events", "pan_events", "motion_level"} <= set(dumped)
    # Honesty contract: unmeasured zoom/pan are labelled, not faked.
    assert dumped["zoom_events"] == "not_estimated"
    assert dumped["pan_events"] == "not_estimated"


def test_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.mp4"
    with pytest.raises(SceneAnalysisError):
        detect_scenes(missing)
    with pytest.raises(SceneAnalysisError):
        estimate_motion(missing)


# ---------------------------------------------------------------------------
# FFmpeg integration: synthetic ground truth
# ---------------------------------------------------------------------------


@ffmpeg_required
def test_detect_scenes_synthetic_hard_cuts(colour_video: Path) -> None:
    scenes = detect_scenes(colour_video)
    # Ground truth: 4 segments, cuts at 2/4/6 s. Tolerance +-1 scene
    # for encoder/scene-score edge effects (in practice it is exact).
    assert 3 <= len(scenes) <= 5
    assert scenes[0].start_s == 0.0
    assert scenes[-1].end_s == pytest.approx(8.0, abs=0.2)
    for scene, expected_cut in zip(scenes[1:], (2.0, 4.0, 6.0)):
        assert scene.start_s == pytest.approx(expected_cut, abs=0.35)
    # Contiguous, no gaps/overlaps, durations sum to the duration.
    for prev, nxt in zip(scenes, scenes[1:]):
        assert prev.end_s == nxt.start_s
    assert sum(s.duration_s for s in scenes) == pytest.approx(8.0, abs=0.2)


@ffmpeg_required
def test_synthetic_aggregates_correct(colour_video: Path) -> None:
    seg = segment_video(colour_video)
    assert seg.low_segmentation is False
    aggs = compute_aggregates(seg.scenes, duration_s=seg.duration_s)
    assert aggs.scene_count == len(seg.scenes)
    assert aggs.avg_shot_length_s == pytest.approx(2.0, abs=0.35)
    assert aggs.cuts_per_minute == pytest.approx(22.5, abs=4.0)


@ffmpeg_required
def test_single_take_fallback_marks_low_segmentation(static_video: Path) -> None:
    seg = segment_video(static_video)
    # No cuts exist and none may be invented: exactly one scene
    # spanning the whole video, flagged low-segmentation.
    assert len(seg.scenes) == 1
    assert seg.scenes[0].start_s == 0.0
    assert seg.scenes[0].end_s == pytest.approx(seg.duration_s, abs=0.05)
    assert seg.duration_s == pytest.approx(25.0, abs=0.2)
    assert seg.low_segmentation is True
    assert any("Low segmentation" in note for note in seg.notes)


@ffmpeg_required
def test_motion_static_is_low(static_video: Path) -> None:
    motion = estimate_motion(static_video)
    assert motion.motion_level == "low"
    assert motion.median_abs_frame_diff == pytest.approx(0.0, abs=0.5)
    assert motion.zoom_events == "not_estimated"
    assert motion.pan_events == "not_estimated"
    assert motion.sampled_frames > 10


@ffmpeg_required
def test_motion_moving_exceeds_static(
    moving_video: Path, static_video: Path
) -> None:
    moving = estimate_motion(moving_video)
    static = estimate_motion(static_video)
    assert moving.motion_level in ("moderate", "high")
    assert (moving.median_abs_frame_diff or 0) > (static.median_abs_frame_diff or 0)
    assert (moving.mean_abs_frame_diff or 0) > 1.0


@ffmpeg_required
def test_motion_cuts_do_not_fake_high_motion(colour_video: Path) -> None:
    # The colour concat is static within scenes but has 3 huge cut
    # spikes: the mean is inflated, the median-based level stays low.
    motion = estimate_motion(colour_video)
    assert motion.motion_level == "low"
    assert (motion.mean_abs_frame_diff or 0) > (motion.median_abs_frame_diff or 0)


@ffmpeg_required
def test_analyse_video_end_to_end(colour_video: Path) -> None:
    analysis = analyse_video(colour_video)
    assert analysis.aggregates.scene_count == len(analysis.scenes)
    assert analysis.duration_s == pytest.approx(8.0, abs=0.2)
    assert analysis.low_segmentation is False
    assert analysis.motion.motion_level == "low"
    assert analysis.threshold == pytest.approx(0.3)
    dumped = analysis.model_dump()
    assert dumped["aggregates"]["scene_count"] == len(analysis.scenes)
    assert dumped["scenes"][0]["index"] == 0
