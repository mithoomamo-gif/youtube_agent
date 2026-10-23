"""Post-concat verification gates and QC by-products.

The renderer's contract: never produce a "probably fine" file — produce a
file PLUS evidence.  Gates (all hard failures):

  (a) total duration: |sum(segment durations) - output duration| > 0.5 s
  (b) ffprobe parameters of the output vs the locked segment contract
  (c) decode check around every join point (decode ~1 s straddling each
      boundary and require a clean decode)

QC by-products (no extra full encode): an ffprobe JSON report and a
contact sheet (tile of sampled frames).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .contract import (
    CONCAT_DURATION_TOLERANCE_S,
    FPS,
    HEIGHT,
    PROFILE,
    WIDTH,
)
from ...ffmpeg_resolution import ffmpeg_executable, ffprobe_executable


class VerificationError(RuntimeError):
    """Raised when a post-concat gate fails."""


def ffprobe(path: Path, ffprobe_bin: str = ffprobe_executable()) -> dict:
    """Run ffprobe and return the parsed JSON report."""
    cmd = [
        ffprobe_bin, "-v", "error",
        "-show_streams", "-show_format", "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise VerificationError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def video_stream(report: dict) -> dict:
    for stream in report.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    raise VerificationError("no video stream found")


def probe_duration(path: Path, ffprobe_bin: str = ffprobe_executable()) -> float:
    report = ffprobe(path, ffprobe_bin)
    fmt_duration = report.get("format", {}).get("duration")
    if fmt_duration is not None:
        return float(fmt_duration)
    stream = video_stream(report)
    return float(stream["duration"])


def _fps_of(stream: dict) -> float:
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    num, _, den = rate.partition("/")
    return float(num) / float(den or 1)


def check_stream_contract(
    path: Path,
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    ffprobe_bin: str = ffprobe_executable(),
) -> dict:
    """Check one file's video stream against the locked contract (gate b).

    Returns the ffprobe report on success; raises VerificationError listing
    every mismatch on failure.  Note: the MPEG-TS container clock (1/90000)
    is expected for segments — the contract timebase is enforced in the
    filter graph (settb), not read back from the TS container.
    """
    report = ffprobe(path, ffprobe_bin)
    stream = video_stream(report)
    problems: list[str] = []
    if stream.get("codec_name") != "h264":
        problems.append(f"codec {stream.get('codec_name')!r} != 'h264'")
    if stream.get("profile") != PROFILE:
        problems.append(f"profile {stream.get('profile')!r} != {PROFILE!r}")
    if (stream.get("width"), stream.get("height")) != (width, height):
        problems.append(
            f"size {stream.get('width')}x{stream.get('height')} != {width}x{height}")
    if abs(_fps_of(stream) - FPS) > 0.01:
        problems.append(f"fps {_fps_of(stream)} != {FPS}")
    if stream.get("pix_fmt") != "yuv420p":
        problems.append(f"pix_fmt {stream.get('pix_fmt')!r} != 'yuv420p'")
    audio = [s for s in report.get("streams", []) if s.get("codec_type") == "audio"]
    if path.suffix == ".ts" and audio:
        problems.append("segment carries an audio stream (segments are video-only)")
    if problems:
        raise VerificationError(f"{path.name}: contract violations: {problems}")
    return report


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class VerificationReport:
    output: str
    gates: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def raise_if_failed(self) -> None:
        failed = [g for g in self.gates if not g.passed]
        if failed:
            summary = "; ".join(f"{g.name}: {g.detail}" for g in failed)
            raise VerificationError(f"post-concat verification FAILED — {summary}")


def _decode_check_at(path: Path, centre_s: float, ffmpeg: str) -> tuple[bool, str]:
    """Decode ~1 s straddling ``centre_s``; clean decode = pass (gate c).

    Uses OUTPUT-side seeking (decode from the start, discard up to the
    window) rather than input seeking: input seeking lands mid-GOP and
    makes the H.264 decoder report spurious reference errors
    (``mmco: unref short failure``) for frames whose references were
    skipped by the seek — an artifact of the seek, not of the join.
    Continuous decoding checks the join exactly as a player sees it.
    Frames are counted via framemd5 so a silently truncated decode
    (exit 0, missing frames) also fails.
    """
    start = max(centre_s - 0.5, 0.0)
    cmd = [
        ffmpeg, "-y", "-v", "error",
        "-i", str(path), "-ss", f"{start:.3f}", "-t", "1.0",
        "-f", "framemd5", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    frames = sum(1 for line in proc.stdout.splitlines()
                 if line and not line.startswith("#"))
    expected = FPS  # 1.0 s window at the contract frame rate
    if proc.returncode != 0 or proc.stderr.strip():
        detail = (proc.stderr.strip().splitlines() or ["decode error"])[0][:200]
        return False, detail
    if frames < int(expected * 0.8):
        return False, f"only {frames} frames decoded in join window (expected ~{expected})"
    return True, f"clean decode ({frames} frames)"


def verify_concat(
    segment_paths: list[Path],
    output: Path,
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    ffmpeg: str = ffmpeg_executable(),
    ffprobe_bin: str = ffprobe_executable(),
) -> VerificationReport:
    """Run gates (a), (b) and (c) on a concatenated output file."""
    report = VerificationReport(output=str(output))

    # (a) duration gate
    seg_total = sum(probe_duration(p, ffprobe_bin) for p in segment_paths)
    out_duration = probe_duration(output, ffprobe_bin)
    diff = abs(seg_total - out_duration)
    report.gates.append(GateResult(
        "duration",
        diff <= CONCAT_DURATION_TOLERANCE_S,
        f"segments sum {seg_total:.3f}s vs output {out_duration:.3f}s "
        f"(diff {diff:.3f}s, tolerance {CONCAT_DURATION_TOLERANCE_S}s)",
    ))

    # (b) stream contract gate
    try:
        check_stream_contract(output, width=width, height=height,
                              ffprobe_bin=ffprobe_bin)
        report.gates.append(GateResult("contract", True, "output matches contract"))
    except VerificationError as exc:
        report.gates.append(GateResult("contract", False, str(exc)))

    # (c) decode check around every join point
    joins: list[str] = []
    cumulative = 0.0
    join_ok = True
    for seg in segment_paths[:-1]:
        cumulative += probe_duration(seg, ffprobe_bin)
        ok, detail = _decode_check_at(output, cumulative, ffmpeg)
        join_ok = join_ok and ok
        joins.append(f"@{cumulative:.2f}s {detail}")
    report.gates.append(GateResult(
        "join_decode", join_ok, "; ".join(joins) if joins else "no joins"))

    return report


# ---------------------------------------------------------------------------
# QC by-products
# ---------------------------------------------------------------------------

def write_probe_report(video: Path, out_json: Path,
                       ffprobe_bin: str = ffprobe_executable()) -> Path:
    """Write the ffprobe JSON report for ``video`` (QC evidence)."""
    report = ffprobe(video, ffprobe_bin)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out_json


def make_contact_sheet(
    video: Path,
    out_png: Path,
    *,
    interval_seconds: float = 10.0,
    columns: int = 4,
    thumb_width: int = 480,
    ffmpeg: str = ffmpeg_executable(),
    ffprobe_bin: str = ffprobe_executable(),
) -> Path:
    """Tile of frames sampled every ``interval_seconds`` (QC evidence).

    One frame per interval, scaled to thumbnails and tiled into a single
    PNG so a reviewer (human or agent) can spot-check scenes at a glance.
    """
    duration = probe_duration(video, ffprobe_bin)
    count = max(1, int(duration // interval_seconds) + 1)
    rows = max(1, -(-count // columns))
    vf = (
        f"fps=1/{interval_seconds},scale={thumb_width}:-2,"
        f"tile={columns}x{rows}"
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(video),
           "-vf", vf, "-frames:v", "1", str(out_png)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out_png.exists():
        raise VerificationError(
            f"contact sheet failed for {video}: {proc.stderr.strip()[:300]}")
    return out_png
