"""Tests for technical QC (Agent 32) -- roadmap section 17, layer L6.

The QC gate is tested like a classifier: a fault-injection corpus of
deliberately broken videos (planted black segment, planted freeze,
planted silence) must be caught, and a clean video must pass.  Media is
generated with the real ffmpeg at tiny size (320x180) so the suite stays
fast.  Loudness verdicts are exercised via injected pre-measured values
(the documented input path); the ffmpeg loudnorm measurement path is
covered separately by asserting a measurement is produced.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.pipeline.gates import Verdict  # noqa: E402
from app.pipeline.qc import (  # noqa: E402
    LoudnessMeasurement,
    MediaContract,
    parse_blackdetect,
    parse_freezedetect,
    parse_silencedetect,
    run_technical_qc,
)

GOOD_LOUDNESS = LoudnessMeasurement(
    integrated_lufs=-14.2, true_peak_dbtp=-3.0, source="provided"
)
CONTRACT_6S = MediaContract(
    expected_duration_s=6.0,
    duration_tolerance_s=0.6,
    expected_width=320,
    expected_height=180,
    expected_fps=30.0,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _ffmpeg(args):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """Build the fault-injection corpus once per module."""
    d = tmp_path_factory.mktemp("qc_media")
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]

    # Clean: moving test pattern + continuous tone, 3 s.
    clean = d / "clean.mp4"
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=duration=3:size=320x180:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        *enc, str(clean),
    ])

    # Planted black segment: 1.5 s picture + 3 s black + 1.5 s picture.
    black = d / "black.mp4"
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=duration=1.5:size=320x180:rate=30",
        "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30:duration=3",
        "-f", "lavfi", "-i", "testsrc2=duration=1.5:size=320x180:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a",
        *enc, str(black),
    ])

    # Planted freeze: 1 s picture + 5 s static red + 1 s picture.
    freeze = d / "freeze.mp4"
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x180:rate=30",
        "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=30:duration=5",
        "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x180:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=7",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a",
        *enc, str(freeze),
    ])

    # Planted silence: tone 1 s + 4 s digital silence + tone 1 s.
    silence = d / "silence.mp4"
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=duration=6:size=320x180:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-filter_complex",
        "[1:a]atrim=0:1[a1];[2:a]atrim=0:4[a2];[3:a]atrim=0:1[a3];"
        "[a1][a2][a3]concat=n=3:v=0:a=1[a]",
        "-map", "0:v", "-map", "[a]",
        *enc, str(silence),
    ])
    return {"clean": clean, "black": black, "freeze": freeze, "silence": silence}


# ---------------------------------------------------------------------------
# Fault injection: the gate must be seen to fail (section 17 rule)
# ---------------------------------------------------------------------------


def test_qc_passes_clean_video(media):
    report = run_technical_qc(
        media["clean"],
        contract=MediaContract(
            expected_duration_s=3.0, duration_tolerance_s=0.6,
            expected_width=320, expected_height=180, expected_fps=30.0,
        ),
        loudness=GOOD_LOUDNESS,
    )
    assert report.verdict is Verdict.PASS, [
        (c.check_id, c.verdict, c.reason_codes) for c in report.checks
    ]


def test_qc_detects_planted_black_segment(media):
    report = run_technical_qc(media["black"], contract=CONTRACT_6S,
                              loudness=GOOD_LOUDNESS)
    black_check = report.check("black_frames")
    assert black_check is not None
    assert black_check.verdict is Verdict.BLOCK
    assert "BLACK_SEGMENT_TOO_LONG" in black_check.reason_codes
    assert report.detections["black"], "blackdetect found nothing"
    longest = max(r.duration_s for r in report.detections["black"])
    assert longest >= 2.5  # planted segment is 3 s
    assert report.verdict is Verdict.BLOCK


def test_qc_detects_planted_freeze(media):
    report = run_technical_qc(
        media["freeze"],
        contract=MediaContract(expected_duration_s=7.0, duration_tolerance_s=0.6),
        loudness=GOOD_LOUDNESS,
    )
    freeze_check = report.check("freeze_frames")
    assert freeze_check is not None
    assert freeze_check.verdict is Verdict.BLOCK
    assert "FREEZE_TOO_LONG" in freeze_check.reason_codes
    assert report.detections["freeze"], "freezedetect found nothing"
    longest = max(r.duration_s for r in report.detections["freeze"])
    assert longest >= 4.0  # planted freeze is 5 s
    assert report.verdict is Verdict.BLOCK


def test_qc_detects_planted_silence(media):
    report = run_technical_qc(media["silence"], contract=CONTRACT_6S,
                              loudness=GOOD_LOUDNESS)
    silence_check = report.check("silence")
    assert silence_check is not None
    assert silence_check.verdict is Verdict.BLOCK
    assert "SILENCE_TOO_LONG" in silence_check.reason_codes
    assert report.verdict is Verdict.BLOCK


def test_qc_declared_still_range_whitelists_freeze(media):
    from app.pipeline.qc import TimeRange

    report = run_technical_qc(
        media["freeze"],
        loudness=GOOD_LOUDNESS,
        declared_still_ranges=(TimeRange(1.0, 6.0),),
    )
    freeze_check = report.check("freeze_frames")
    assert freeze_check.verdict is Verdict.PASS
    assert "FREEZE_DECLARED_STILL" in freeze_check.reason_codes


# ---------------------------------------------------------------------------
# Contract + loudness rules (injected measurements)
# ---------------------------------------------------------------------------


def test_qc_blocks_duration_mismatch(media):
    report = run_technical_qc(
        media["clean"],
        contract=MediaContract(expected_duration_s=30.0),
        loudness=GOOD_LOUDNESS,
    )
    assert report.check("duration").verdict is Verdict.BLOCK
    assert report.verdict is Verdict.BLOCK


def test_qc_blocks_resolution_mismatch(media):
    report = run_technical_qc(
        media["clean"],
        contract=MediaContract(expected_width=1920, expected_height=1080),
        loudness=GOOD_LOUDNESS,
    )
    assert report.check("resolution").verdict is Verdict.BLOCK


@pytest.mark.parametrize(
    "loudness,expected",
    [
        (LoudnessMeasurement(-14.2, -3.0), Verdict.PASS),
        (LoudnessMeasurement(-15.5, -3.0), Verdict.WARN),   # 1.5 LU off target
        (LoudnessMeasurement(-20.0, -3.0), Verdict.BLOCK),  # far too quiet
        (LoudnessMeasurement(-8.0, -3.0), Verdict.BLOCK),   # far too loud
        (LoudnessMeasurement(-14.0, -0.5), Verdict.BLOCK),  # true peak > -1 dBTP
    ],
)
def test_qc_loudness_rules(media, loudness, expected):
    report = run_technical_qc(media["clean"], loudness=loudness)
    assert report.check("loudness").verdict is expected


def test_qc_loudness_measured_by_ffmpeg_when_not_provided(media):
    report = run_technical_qc(media["clean"])
    assert report.loudness is not None
    assert report.loudness.source == "loudnorm"
    assert -60.0 < report.loudness.integrated_lufs < 0.0


def test_qc_missing_file_blocks():
    report = run_technical_qc("/nonexistent/final.mp4", loudness=GOOD_LOUDNESS)
    assert report.verdict is Verdict.BLOCK
    assert report.checks[0].reason_codes == ("MEDIA_NOT_FOUND",)


def test_qc_report_emits_bound_token(media):
    from app.pipeline.gates import compute_manifest_hash, verify_token_binding

    manifest_hash = compute_manifest_hash({"video": "clean"})
    report = run_technical_qc(
        media["clean"],
        contract=MediaContract(
            expected_duration_s=3.0, duration_tolerance_s=0.6,
            expected_width=320, expected_height=180, expected_fps=30.0,
        ),
        loudness=GOOD_LOUDNESS,
    )
    token = report.to_token(manifest_hash)
    assert token.gate_id == "qc.technical"
    assert token.verdict is Verdict.PASS
    assert verify_token_binding(token, manifest_hash)
    assert not verify_token_binding(token, compute_manifest_hash({"video": "x"}))


# ---------------------------------------------------------------------------
# Parser unit tests (synthetic ffmpeg stderr)
# ---------------------------------------------------------------------------


def test_parse_blackdetect():
    stderr = (
        "[blackdetect @ 0x1] black_start:1.5 black_end:4.5 black_duration:3\n"
        "[blackdetect @ 0x1] black_start:8 black_end:8.4 black_duration:0.4\n"
    )
    ranges = parse_blackdetect(stderr)
    assert len(ranges) == 2
    assert ranges[0].duration_s == 3.0


def test_parse_freezedetect_pairs_events_and_closes_at_eof():
    stderr = (
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 1\n"
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_duration: 5\n"
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_end: 6\n"
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 9\n"
    )
    ranges = parse_freezedetect(stderr, media_duration_s=12.0)
    assert [(r.start_s, r.end_s) for r in ranges] == [(1.0, 6.0), (9.0, 12.0)]


def test_parse_silencedetect():
    stderr = (
        "silence_start: 1.0\n"
        "silence_end: 5.0 | silence_duration: 4.0\n"
    )
    ranges = parse_silencedetect(stderr)
    assert len(ranges) == 1 and ranges[0].duration_s == 4.0
