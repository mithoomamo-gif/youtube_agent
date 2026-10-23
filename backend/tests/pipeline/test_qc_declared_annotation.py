"""Tests for planner pre-declarations -> QC annotation (Round 4).

Hard rules under test (annotation is powerless by design):

* a declaration NEVER suppresses/removes a flag or reason code;
* a declaration NEVER changes a check verdict or the report verdict
  (a declared-dark brief black segment is still WARN, a declared
  block-length black segment is still BLOCK);
* a declaration NEVER changes any threshold;
* it ONLY adds annotation keys to the flagged range record (and a
  summary on the check's details).

Unit tests drive the real classifier (``_classify_ranges``) with
synthetic ranges — no ffmpeg needed.  Integration tests run the real
``run_technical_qc`` on ffmpeg-generated fault media with a *brief*
( WARN-level ) planted black segment / freeze.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.pipeline.gates import Verdict  # noqa: E402
from app.pipeline.qc import LoudnessMeasurement, run_technical_qc  # noqa: E402
from app.pipeline.qc.declarations import (  # noqa: E402
    FLAG_CODE_FOR_CONDITION,
    SceneDeclaration,
    annotate_check_result,
    annotate_qc_report,
    declarations_from_scenes,
)
from app.pipeline.qc.technical import TimeRange, _classify_ranges  # noqa: E402
from app.pipeline.scenes import DeclaredCondition, SceneClass, SceneRecord, SourceKind  # noqa: E402

GOOD_LOUDNESS = LoudnessMeasurement(
    integrated_lufs=-14.2, true_peak_dbtp=-3.0, source="provided"
)


def make_scene(scene_id="s1", start=0.0, end=6.0, declared=()):
    return SceneRecord(
        scene_id=scene_id,
        source_sentence=f"Sentence for {scene_id}.",
        start_s=start,
        end_s=end,
        scene_class=SceneClass.REAL_WORLD,
        chosen_source=SourceKind.STOCK,
        declared_conditions=list(declared),
    )


def black_check(duration=1.0, start=2.5):
    """Real classifier output for one black range (default: brief/WARN)."""
    return _classify_ranges(
        [TimeRange(start, start + duration)],
        warn_min_s=0.5,
        block_min_s=2.0,
        declared=(),
        block_code="BLACK_SEGMENT_TOO_LONG",
        warn_code="BLACK_SEGMENT_BRIEF",
        declared_code="",
    )


def freeze_check(duration=3.0, start=1.0):
    """Real classifier output for one freeze range (default: WARN)."""
    return _classify_ranges(
        [TimeRange(start, start + duration)],
        warn_min_s=2.0,
        block_min_s=4.0,
        declared=(),
        block_code="FREEZE_TOO_LONG",
        warn_code="FREEZE_DETECTED",
        declared_code="FREEZE_DECLARED_STILL",
    )


# ---------------------------------------------------------------------------
# Declaration model
# ---------------------------------------------------------------------------


def test_scene_declared_conditions_default_empty_and_unaffected():
    scene = make_scene()
    assert scene.declared_conditions == []


def test_scene_declared_conditions_accept_dark_and_still():
    scene = make_scene(declared=["dark", "still"])
    assert scene.declared_conditions == [
        DeclaredCondition.DARK,
        DeclaredCondition.STILL,
    ]
    # Mapping required by the spec: dark -> BLACK_SEGMENT_BRIEF,
    # still -> FREEZE_DECLARED_STILL.
    assert FLAG_CODE_FOR_CONDITION[DeclaredCondition.DARK] == "BLACK_SEGMENT_BRIEF"
    assert FLAG_CODE_FOR_CONDITION[DeclaredCondition.STILL] == "FREEZE_DECLARED_STILL"


def test_scene_declared_conditions_reject_unknown_value():
    with pytest.raises(Exception):
        make_scene(declared=["loud"])


def test_declarations_from_scenes_skips_undeclared_and_reads_dicts():
    scenes = [
        make_scene("plain", 0.0, 5.0),
        make_scene("dark1", 5.0, 9.0, declared=["dark"]),
        {"scene_id": "still1", "start_s": 9.0, "end_s": 12.0,
         "declared_conditions": ["still"]},
        SceneDeclaration(12.0, 15.0, ("dark",), scene_id="direct"),
    ]
    decls = declarations_from_scenes(scenes)
    assert [d.scene_id for d in decls] == ["dark1", "still1", "direct"]
    assert decls[0].declares(DeclaredCondition.DARK)
    assert decls[1].declares(DeclaredCondition.STILL)


# ---------------------------------------------------------------------------
# Annotation: declared cases
# ---------------------------------------------------------------------------


def test_declared_dark_black_segment_flag_present_verdict_still_warn():
    check = black_check()
    assert check.verdict is Verdict.WARN
    assert "BLACK_SEGMENT_BRIEF" in check.reason_codes

    annotated = annotate_check_result(
        check, declarations_from_scenes([make_scene("s1", 2.0, 4.0, ["dark"])])
    )

    # Flag + verdict byte-identical; only annotation keys were added.
    assert annotated.verdict is Verdict.WARN
    assert annotated.reason_codes == check.reason_codes
    assert "BLACK_SEGMENT_BRIEF" in annotated.reason_codes
    entry = annotated.details["ranges"][0]
    assert entry["classification"] == "warn"
    assert entry["declared"] is True
    assert entry["declared_condition"] == "dark"
    assert entry["note"] == "pre-declared by planner"
    assert entry["declared_scene_id"] == "s1"
    assert annotated.details["declared"] is True
    assert annotated.details["declared_condition"] == "dark"
    # Input check untouched (annotation returns a new object).
    assert "declared" not in check.details["ranges"][0]


def test_declared_still_freeze_flag_annotated_verdict_still_warn():
    check = freeze_check()
    assert check.verdict is Verdict.WARN
    annotated = annotate_check_result(
        check, declarations_from_scenes([make_scene("s2", 0.5, 4.5, ["still"])])
    )
    assert annotated.verdict is Verdict.WARN
    assert annotated.reason_codes == check.reason_codes
    assert "FREEZE_DETECTED" in annotated.reason_codes
    entry = annotated.details["ranges"][0]
    assert entry["declared"] is True
    assert entry["declared_condition"] == "still"
    assert entry["note"] == "pre-declared by planner"


# ---------------------------------------------------------------------------
# Annotation: undeclared / mismatched cases (consistent declared=False)
# ---------------------------------------------------------------------------


def test_black_segment_without_declaration_has_no_declared_annotation():
    check = black_check()
    annotated = annotate_check_result(check, declarations_from_scenes([]))
    entry = annotated.details["ranges"][0]
    assert entry["declared"] is False
    assert "declared_condition" not in entry
    assert "note" not in entry
    assert annotated.details["declared"] is False
    assert annotated.verdict is Verdict.WARN
    assert annotated.reason_codes == check.reason_codes


def test_declaration_outside_segment_timing_does_not_annotate():
    annotated = annotate_check_result(
        black_check(),
        declarations_from_scenes([make_scene("far", 20.0, 25.0, ["dark"])]),
    )
    assert annotated.details["ranges"][0]["declared"] is False


def test_wrong_condition_does_not_annotate():
    # "still" does not pre-declare black; "dark" does not pre-declare freeze.
    dark_only = declarations_from_scenes([make_scene("s1", 0.0, 10.0, ["dark"])])
    still_only = declarations_from_scenes([make_scene("s1", 0.0, 10.0, ["still"])])
    assert annotate_check_result(freeze_check(), dark_only).details["ranges"][0]["declared"] is False
    assert annotate_check_result(black_check(), still_only).details["ranges"][0]["declared"] is False


def test_non_annotatable_check_returned_unchanged():
    from app.pipeline.qc.technical import CheckResult

    check = CheckResult(
        check_id="loudness", verdict=Verdict.PASS, details={"x": 1}
    )
    decls = declarations_from_scenes([make_scene("s1", 0.0, 10.0, ["dark", "still"])])
    assert annotate_check_result(check, decls) is check


# ---------------------------------------------------------------------------
# Annotation never changes verdict / never removes the flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_check,duration,expected_verdict,expected_code",
    [
        (black_check, 0.4, Verdict.PASS, None),               # brief_ignored
        (black_check, 1.0, Verdict.WARN, "BLACK_SEGMENT_BRIEF"),
        (black_check, 3.0, Verdict.BLOCK, "BLACK_SEGMENT_TOO_LONG"),
        (freeze_check, 3.0, Verdict.WARN, "FREEZE_DETECTED"),
        (freeze_check, 5.0, Verdict.BLOCK, "FREEZE_TOO_LONG"),
    ],
)
def test_annotation_never_changes_verdict_or_codes(
    make_check, duration, expected_verdict, expected_code
):
    check = make_check(duration=duration)
    assert check.verdict is expected_verdict
    decls = declarations_from_scenes(
        [make_scene("s1", 0.0, 30.0, ["dark", "still"])]
    )
    annotated = annotate_check_result(check, decls)
    assert annotated.verdict is expected_verdict  # WARN never becomes PASS, etc.
    assert annotated.reason_codes == check.reason_codes
    if expected_code is not None:
        assert expected_code in annotated.reason_codes  # flag never removed
    # Even a BLOCK range inside a declared scene is only annotated.
    entry = annotated.details["ranges"][0]
    assert entry["declared"] is True
    assert entry["classification"] == check.details["ranges"][0]["classification"]


def test_annotate_qc_report_keeps_report_verdict():
    from app.pipeline.qc.technical import QCReport

    report = QCReport(media_path="x.mp4", checks=(black_check(), freeze_check()))
    assert report.verdict is Verdict.WARN
    decls = declarations_from_scenes([make_scene("s1", 0.0, 30.0, ["dark", "still"])])
    annotated = annotate_qc_report(report, decls)
    assert annotated.verdict is Verdict.WARN
    assert annotated.check("black_frames").details["ranges"][0]["declared"] is True
    assert annotated.check("freeze_frames").details["ranges"][0]["declared"] is True


# ---------------------------------------------------------------------------
# Integration: real run_technical_qc on planted WARN-level faults
# ---------------------------------------------------------------------------

pytestmark_media = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _ffmpeg(args):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture(scope="module")
def brief_media(tmp_path_factory):
    d = tmp_path_factory.mktemp("qc_declared_media")
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    # Brief black: 2.5 s picture + 1.0 s black + 2.5 s picture (WARN).
    black = d / "brief_black.mp4"
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=duration=2.5:size=320x180:rate=30",
        "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30:duration=1",
        "-f", "lavfi", "-i", "testsrc2=duration=2.5:size=320x180:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a", *enc, str(black),
    ])
    # Brief freeze: 1 s picture + 3 s static red + 1 s picture (WARN).
    freeze = d / "brief_freeze.mp4"
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x180:rate=30",
        "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=30:duration=3",
        "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x180:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a", *enc, str(freeze),
    ])
    return {"black": black, "freeze": freeze}


@pytestmark_media
def test_run_qc_declared_dark_annotates_and_still_warns(brief_media):
    scene = make_scene("dark-scene", 2.0, 4.0, declared=["dark"])
    report = run_technical_qc(
        brief_media["black"], loudness=GOOD_LOUDNESS, declared_scenes=[scene]
    )
    check = report.check("black_frames")
    assert check is not None
    assert check.verdict is Verdict.WARN  # annotation did NOT turn it PASS
    assert "BLACK_SEGMENT_BRIEF" in check.reason_codes  # flag not removed
    flagged = [r for r in check.details["ranges"] if r["classification"] == "warn"]
    assert flagged, check.details
    assert flagged[0]["declared"] is True
    assert flagged[0]["declared_condition"] == "dark"
    assert flagged[0]["note"] == "pre-declared by planner"


@pytestmark_media
def test_run_qc_undeclared_black_is_declared_false(brief_media):
    report = run_technical_qc(brief_media["black"], loudness=GOOD_LOUDNESS)
    check = report.check("black_frames")
    assert check.verdict is Verdict.WARN
    assert "BLACK_SEGMENT_BRIEF" in check.reason_codes
    flagged = [r for r in check.details["ranges"] if r["classification"] == "warn"]
    assert flagged
    assert flagged[0]["declared"] is False
    assert "declared_condition" not in flagged[0]


@pytestmark_media
def test_run_qc_declared_still_annotates_freeze_and_still_warns(brief_media):
    scene = make_scene("still-scene", 0.5, 4.5, declared=["still"])
    report = run_technical_qc(
        brief_media["freeze"], loudness=GOOD_LOUDNESS, declared_scenes=[scene]
    )
    check = report.check("freeze_frames")
    assert check is not None
    assert check.verdict is Verdict.WARN
    assert "FREEZE_DETECTED" in check.reason_codes
    flagged = [r for r in check.details["ranges"] if r["classification"] == "warn"]
    assert flagged, check.details
    assert flagged[0]["declared"] is True
    assert flagged[0]["declared_condition"] == "still"
    assert flagged[0]["note"] == "pre-declared by planner"
