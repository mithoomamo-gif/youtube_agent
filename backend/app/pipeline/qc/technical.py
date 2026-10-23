"""Technical QC for the final rendered video (Agent 32).

Real FFmpeg-based checks on the final muxed file, mirroring roadmap
sections 17 (L6 render QC) and 18 (render-side QC hooks):

* Tier 1 -- ffprobe structural probe: streams present, duration vs the
  expected duration (+- tolerance), resolution / fps contract.
* Tier 2 -- ONE decode pass chaining ``blackdetect`` + ``freezedetect``
  (video) and ``silencedetect`` (audio); a rules layer converts raw
  detections into PASS / WARN / BLOCK by duration (brief black at a scene
  join is a WARN-level event; a long black segment BLOCKs).
* Tier 3 -- loudness.  The QC gate RE-MEASURES the final file
  independently (a stored loudnorm measurement is a claim, not
  verification).  Callers may instead pass a pre-measured
  :class:`LoudnessMeasurement` -- e.g. measured by the render stage with
  the same ffmpeg build -- via the ``loudness`` argument, in which case
  the measurement pass is skipped.  If no loudness value is available at
  all, the loudness check BLOCKs (fail-closed) rather than guessing.

Declared exceptions are inputs, not loopholes: ``declared_still_ranges``
are scenes the manifest declares as stills (section 18: freezedetect must
whitelist them) and ``declared_silence_ranges`` are declared intro/outro
windows; detections >= 80% inside a declared range are reported but do
not escalate.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from ..gates.tokens import GATE_QC_TECHNICAL, GateToken, Verdict, issue_token, worst_verdict
from .declarations import annotate_qc_checks, declarations_from_scenes
from ...ffmpeg_resolution import ffmpeg_executable, ffprobe_executable

__all__ = [
    "TimeRange",
    "LoudnessMeasurement",
    "MediaContract",
    "CheckResult",
    "QCReport",
    "run_technical_qc",
    "measure_loudness",
    "probe_media",
    "parse_blackdetect",
    "parse_freezedetect",
    "parse_silencedetect",
    "parse_loudnorm_json",
    # thresholds
    "BLACK_WARN_MIN_S",
    "BLACK_BLOCK_MIN_S",
    "FREEZE_WARN_MIN_S",
    "FREEZE_BLOCK_MIN_S",
    "SILENCE_WARN_MIN_S",
    "SILENCE_BLOCK_MIN_S",
    "LOUDNESS_TARGET_LUFS",
    "LOUDNESS_PASS_TOLERANCE_LU",
    "LOUDNESS_WARN_TOLERANCE_LU",
    "TRUE_PEAK_BLOCK_DBTP",
    "DEFAULT_DURATION_TOLERANCE_S",
    "FPS_TOLERANCE",
]

# ---------------------------------------------------------------------------
# Thresholds (named constants -- QC rules layer, roadmap section 17 L6)
# ---------------------------------------------------------------------------

BLACK_WARN_MIN_S = 0.5  # black >= this (and < BLOCK) -> WARN
BLACK_BLOCK_MIN_S = 2.0  # black >= this -> BLOCK (mid-scene black = failed render)
FREEZE_WARN_MIN_S = 2.0  # undeclared freeze >= this (and < BLOCK) -> WARN
FREEZE_BLOCK_MIN_S = 4.0  # undeclared freeze >= this -> BLOCK
SILENCE_WARN_MIN_S = 1.5  # undeclared silence >= this (and < BLOCK) -> WARN
SILENCE_BLOCK_MIN_S = 3.0  # undeclared silence >= this -> BLOCK (failed TTS chunk)

LOUDNESS_TARGET_LUFS = -14.0  # YouTube programme loudness target
LOUDNESS_PASS_TOLERANCE_LU = 1.0  # -14 +- 1 LUFS -> PASS
LOUDNESS_WARN_TOLERANCE_LU = 2.0  # -14 +- 2 LUFS -> WARN; beyond -> BLOCK
TRUE_PEAK_BLOCK_DBTP = -1.0  # true peak above this -> BLOCK

DEFAULT_DURATION_TOLERANCE_S = 0.5
FPS_TOLERANCE = 0.6

# Detection filter tuning (minimum durations are kept low so the RULES
# layer above -- not ffmpeg -- decides what counts).
_BLACKDETECT_MIN_S = 0.3
_FREEZEDETECT_MIN_S = 1.0
_SILENCEDETECT_MIN_S = 0.5
_SILENCE_NOISE = "-50dB"
_FREEZE_NOISE = "-60dB"
_DECLARED_OVERLAP_FRACTION = 0.8


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeRange:
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass(frozen=True)
class LoudnessMeasurement:
    integrated_lufs: float
    true_peak_dbtp: float
    lra_lu: Optional[float] = None
    source: str = "provided"  # "provided" | "loudnorm"


@dataclass(frozen=True)
class MediaContract:
    """What the final file is supposed to be."""

    expected_duration_s: Optional[float] = None
    duration_tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S
    expected_width: Optional[int] = None
    expected_height: Optional[int] = None
    expected_fps: Optional[float] = None
    fps_tolerance: float = FPS_TOLERANCE
    require_video: bool = True
    require_audio: bool = True


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    verdict: Verdict
    reason_codes: tuple = ()
    details: Mapping = field(default_factory=dict)


@dataclass
class QCReport:
    media_path: str
    checks: tuple  # tuple[CheckResult, ...]
    probe: Mapping = field(default_factory=dict)
    detections: Mapping = field(default_factory=dict)  # black/freeze/silence -> [TimeRange]
    loudness: Optional[LoudnessMeasurement] = None

    @property
    def verdict(self) -> Verdict:
        return worst_verdict(c.verdict for c in self.checks)

    def check(self, check_id: str) -> Optional[CheckResult]:
        for result in self.checks:
            if result.check_id == check_id:
                return result
        return None

    def to_token(self, manifest_hash: str, **kwargs) -> GateToken:
        return issue_token(
            GATE_QC_TECHNICAL,
            self.verdict,
            manifest_hash,
            reason_codes=tuple(
                code for c in self.checks for code in c.reason_codes
            ),
            evidence={
                "media_path": self.media_path,
                "checks": {
                    c.check_id: c.verdict.value for c in self.checks
                },
                "detections": {
                    name: [
                        {"start_s": r.start_s, "end_s": r.end_s}
                        for r in ranges
                    ]
                    for name, ranges in self.detections.items()
                },
                "loudness": (
                    {
                        "integrated_lufs": self.loudness.integrated_lufs,
                        "true_peak_dbtp": self.loudness.true_peak_dbtp,
                        "source": self.loudness.source,
                    }
                    if self.loudness is not None
                    else None
                ),
            },
            **kwargs,
        )


# ---------------------------------------------------------------------------
# ffprobe / ffmpeg plumbing
# ---------------------------------------------------------------------------


def _run(cmd: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd), capture_output=True, text=True, timeout=timeout, check=False
    )


def probe_media(path: Path, ffprobe_bin: str = ffprobe_executable(), timeout: float = 60.0) -> dict:
    """ffprobe JSON (streams + format). Raises RuntimeError on failure."""
    proc = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


def _stream_fps(stream: Mapping) -> Optional[float]:
    raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or ""
    try:
        num, _, den = raw.partition("/")
        return float(num) / float(den) if den and float(den) else float(num)
    except (ValueError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------
# Detection-output parsers (pure functions -- unit-testable without ffmpeg)
# ---------------------------------------------------------------------------

_BLACK_RE = re.compile(
    r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)"
)
_FREEZE_START_RE = re.compile(r"lavfi\.freezedetect\.freeze_start:\s*([\d.]+)")
_FREEZE_END_RE = re.compile(r"lavfi\.freezedetect\.freeze_end:\s*([\d.]+)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*([\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([\d.]+)")
_LOUDNORM_JSON_RE = re.compile(r'\{[^{}]*"input_i"[^{}]*\}', re.DOTALL)


def parse_blackdetect(stderr: str) -> list:
    return [
        TimeRange(start_s=float(m.group(1)), end_s=float(m.group(2)))
        for m in _BLACK_RE.finditer(stderr)
    ]


def parse_freezedetect(stderr: str, media_duration_s: Optional[float] = None) -> list:
    """Pair freeze_start/freeze_end events.  A freeze still running at
    end-of-stream is closed at ``media_duration_s`` when known."""
    events = []
    for line in stderr.splitlines():
        m = _FREEZE_START_RE.search(line)
        if m:
            events.append(("start", float(m.group(1))))
            continue
        m = _FREEZE_END_RE.search(line)
        if m:
            events.append(("end", float(m.group(1))))
    ranges = []
    open_start: Optional[float] = None
    for kind, t in events:
        if kind == "start":
            open_start = t
        elif open_start is not None:
            ranges.append(TimeRange(start_s=open_start, end_s=t))
            open_start = None
    if open_start is not None and media_duration_s is not None:
        ranges.append(TimeRange(start_s=open_start, end_s=float(media_duration_s)))
    return ranges


def parse_silencedetect(stderr: str, media_duration_s: Optional[float] = None) -> list:
    events = []
    for line in stderr.splitlines():
        m = _SILENCE_START_RE.search(line)
        if m:
            events.append(("start", float(m.group(1))))
            continue
        m = _SILENCE_END_RE.search(line)
        if m:
            events.append(("end", float(m.group(1))))
    ranges = []
    open_start: Optional[float] = None
    for kind, t in events:
        if kind == "start":
            open_start = t
        elif open_start is not None:
            ranges.append(TimeRange(start_s=open_start, end_s=t))
            open_start = None
    if open_start is not None and media_duration_s is not None:
        ranges.append(TimeRange(start_s=open_start, end_s=float(media_duration_s)))
    return ranges


def parse_loudnorm_json(stderr: str) -> Optional[LoudnessMeasurement]:
    matches = _LOUDNORM_JSON_RE.findall(stderr)
    if not matches:
        return None
    try:
        data = json.loads(matches[-1])
        return LoudnessMeasurement(
            integrated_lufs=float(data["input_i"]),
            true_peak_dbtp=float(data["input_tp"]),
            lra_lu=float(data["input_lra"]) if data.get("input_lra") else None,
            source="loudnorm",
        )
    except (ValueError, KeyError):
        return None


def measure_loudness(
    path: Path, ffmpeg_bin: str = ffmpeg_executable(), timeout: float = 600.0
) -> Optional[LoudnessMeasurement]:
    """Loudnorm analysis pass (measure-only) on the final file."""
    proc = _run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-vn",
            "-af",
            "loudnorm=I=-14:TP=-1.0:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        timeout,
    )
    return parse_loudnorm_json(proc.stderr)


def _run_detection_pass(
    path: Path, ffmpeg_bin: str, timeout: float
) -> str:
    """One decode pass: blackdetect + freezedetect + silencedetect."""
    vf = (
        f"blackdetect=d={_BLACKDETECT_MIN_S}:pix_th=0.10,"
        f"freezedetect=n={_FREEZE_NOISE}:d={_FREEZEDETECT_MIN_S}"
    )
    af = f"silencedetect=n={_SILENCE_NOISE}:d={_SILENCEDETECT_MIN_S}"
    proc = _run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-vf",
            vf,
            "-af",
            af,
            "-f",
            "null",
            "-",
        ],
        timeout,
    )
    return proc.stderr


# ---------------------------------------------------------------------------
# Rules layer: detections -> verdicts
# ---------------------------------------------------------------------------


def _overlap_fraction(inner: TimeRange, outers: Iterable[TimeRange]) -> float:
    if inner.duration_s <= 0:
        return 0.0
    covered = 0.0
    for outer in outers:
        covered += max(
            0.0, min(inner.end_s, outer.end_s) - max(inner.start_s, outer.start_s)
        )
    return min(1.0, covered / inner.duration_s)


def _classify_ranges(
    ranges: Sequence[TimeRange],
    *,
    warn_min_s: float,
    block_min_s: float,
    declared: Sequence[TimeRange],
    block_code: str,
    warn_code: str,
    declared_code: str,
) -> CheckResult:
    verdict = Verdict.PASS
    reason_codes: list = []
    classified = []
    for r in ranges:
        entry = {"start_s": r.start_s, "end_s": r.end_s, "duration_s": round(r.duration_s, 3)}
        if _overlap_fraction(r, declared) >= _DECLARED_OVERLAP_FRACTION:
            entry["classification"] = "declared"
            if declared_code not in reason_codes:
                reason_codes.append(declared_code)
        elif r.duration_s >= block_min_s:
            entry["classification"] = "block"
            verdict = Verdict.BLOCK
            if block_code not in reason_codes:
                reason_codes.append(block_code)
        elif r.duration_s >= warn_min_s:
            entry["classification"] = "warn"
            if verdict is Verdict.PASS:
                verdict = Verdict.WARN
            if warn_code not in reason_codes:
                reason_codes.append(warn_code)
        else:
            entry["classification"] = "brief_ignored"
        classified.append(entry)
    check_id = block_code.split("_")[0].lower() + "_frames"
    if block_code.startswith("SILENCE"):
        check_id = "silence"
    return CheckResult(
        check_id=check_id,
        verdict=verdict,
        reason_codes=tuple(reason_codes),
        details={"ranges": classified},
    )


def _loudness_check(measurement: Optional[LoudnessMeasurement]) -> CheckResult:
    if measurement is None:
        return CheckResult(
            check_id="loudness",
            verdict=Verdict.BLOCK,
            reason_codes=("LOUDNESS_NOT_MEASURED",),
            details={"note": "no loudness value available; failing closed"},
        )
    deviation = abs(measurement.integrated_lufs - LOUDNESS_TARGET_LUFS)
    reason_codes: list = []
    verdict = Verdict.PASS
    if measurement.true_peak_dbtp > TRUE_PEAK_BLOCK_DBTP:
        verdict = Verdict.BLOCK
        reason_codes.append("TRUE_PEAK_TOO_HIGH")
    if deviation > LOUDNESS_WARN_TOLERANCE_LU:
        verdict = Verdict.BLOCK
        reason_codes.append("LOUDNESS_OUT_OF_RANGE")
    elif deviation > LOUDNESS_PASS_TOLERANCE_LU:
        if verdict is Verdict.PASS:
            verdict = Verdict.WARN
        reason_codes.append("LOUDNESS_OFF_TARGET")
    return CheckResult(
        check_id="loudness",
        verdict=verdict,
        reason_codes=tuple(reason_codes),
        details={
            "integrated_lufs": measurement.integrated_lufs,
            "true_peak_dbtp": measurement.true_peak_dbtp,
            "lra_lu": measurement.lra_lu,
            "target_lufs": LOUDNESS_TARGET_LUFS,
            "source": measurement.source,
        },
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_technical_qc(
    media_path,
    *,
    contract: Optional[MediaContract] = None,
    loudness: Optional[LoudnessMeasurement] = None,
    measure_loudness_if_missing: bool = True,
    declared_still_ranges: Sequence[TimeRange] = (),
    declared_silence_ranges: Sequence[TimeRange] = (),
    declared_scenes: Sequence = (),
    ffmpeg_bin: str = ffmpeg_executable(),
    ffprobe_bin: str = ffprobe_executable(),
    timeout: float = 600.0,
) -> QCReport:
    """Run the full technical QC suite on ``media_path``.

    ``loudness``: pass a pre-measured value to skip the loudnorm pass;
    pass nothing and the gate measures the file itself.
    """
    contract = contract or MediaContract()
    path = Path(media_path)
    checks: list = []

    def fail_report(code: str, detail: str) -> QCReport:
        return QCReport(
            media_path=str(path),
            checks=(
                CheckResult(
                    check_id="probe",
                    verdict=Verdict.BLOCK,
                    reason_codes=(code,),
                    details={"error": detail},
                ),
            ),
        )

    if not path.is_file():
        return fail_report("MEDIA_NOT_FOUND", f"file does not exist: {path}")
    if shutil.which(ffprobe_bin) is None:
        return fail_report("QC_TOOL_MISSING", f"ffprobe not found: {ffprobe_bin}")

    try:
        probe = probe_media(path, ffprobe_bin=ffprobe_bin, timeout=min(timeout, 120.0))
    except (RuntimeError, json.JSONDecodeError) as exc:
        return fail_report("PROBE_FAILED", str(exc))

    streams = probe.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    try:
        duration_s = float(probe.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        duration_s = 0.0

    # -- streams ----------------------------------------------------------
    stream_reasons = []
    if contract.require_video and not video_streams:
        stream_reasons.append("VIDEO_STREAM_MISSING")
    if contract.require_audio and not audio_streams:
        stream_reasons.append("AUDIO_STREAM_MISSING")
    checks.append(
        CheckResult(
            check_id="streams",
            verdict=Verdict.BLOCK if stream_reasons else Verdict.PASS,
            reason_codes=tuple(stream_reasons),
            details={
                "video_streams": len(video_streams),
                "audio_streams": len(audio_streams),
            },
        )
    )

    # -- duration ---------------------------------------------------------
    if contract.expected_duration_s is not None:
        delta = abs(duration_s - contract.expected_duration_s)
        ok = delta <= contract.duration_tolerance_s
        checks.append(
            CheckResult(
                check_id="duration",
                verdict=Verdict.PASS if ok else Verdict.BLOCK,
                reason_codes=() if ok else ("DURATION_MISMATCH",),
                details={
                    "actual_s": duration_s,
                    "expected_s": contract.expected_duration_s,
                    "tolerance_s": contract.duration_tolerance_s,
                },
            )
        )

    # -- resolution / fps contract ---------------------------------------
    if video_streams:
        vs = video_streams[0]
        width, height = vs.get("width"), vs.get("height")
        res_reasons = []
        if contract.expected_width is not None and width != contract.expected_width:
            res_reasons.append("RESOLUTION_WIDTH_MISMATCH")
        if contract.expected_height is not None and height != contract.expected_height:
            res_reasons.append("RESOLUTION_HEIGHT_MISMATCH")
        checks.append(
            CheckResult(
                check_id="resolution",
                verdict=Verdict.BLOCK if res_reasons else Verdict.PASS,
                reason_codes=tuple(res_reasons),
                details={"width": width, "height": height},
            )
        )
        fps = _stream_fps(vs)
        if contract.expected_fps is not None:
            fps_ok = fps is not None and abs(fps - contract.expected_fps) <= contract.fps_tolerance
            checks.append(
                CheckResult(
                    check_id="fps",
                    verdict=Verdict.PASS if fps_ok else Verdict.BLOCK,
                    reason_codes=() if fps_ok else ("FPS_MISMATCH",),
                    details={"actual_fps": fps, "expected_fps": contract.expected_fps},
                )
            )

    # -- detection pass ----------------------------------------------------
    detections: dict = {"black": [], "freeze": [], "silence": []}
    if shutil.which(ffmpeg_bin) is None:
        checks.append(
            CheckResult(
                check_id="detection_pass",
                verdict=Verdict.BLOCK,
                reason_codes=("QC_TOOL_MISSING",),
                details={"error": f"ffmpeg not found: {ffmpeg_bin}"},
            )
        )
    else:
        stderr = _run_detection_pass(path, ffmpeg_bin, timeout)
        detections = {
            "black": parse_blackdetect(stderr),
            "freeze": parse_freezedetect(stderr, duration_s),
            "silence": parse_silencedetect(stderr, duration_s),
        }
        checks.append(
            _classify_ranges(
                detections["black"],
                warn_min_s=BLACK_WARN_MIN_S,
                block_min_s=BLACK_BLOCK_MIN_S,
                declared=(),
                block_code="BLACK_SEGMENT_TOO_LONG",
                warn_code="BLACK_SEGMENT_BRIEF",
                declared_code="",
            )
        )
        checks.append(
            _classify_ranges(
                detections["freeze"],
                warn_min_s=FREEZE_WARN_MIN_S,
                block_min_s=FREEZE_BLOCK_MIN_S,
                declared=declared_still_ranges,
                block_code="FREEZE_TOO_LONG",
                warn_code="FREEZE_DETECTED",
                declared_code="FREEZE_DECLARED_STILL",
            )
        )
        checks.append(
            _classify_ranges(
                detections["silence"],
                warn_min_s=SILENCE_WARN_MIN_S,
                block_min_s=SILENCE_BLOCK_MIN_S,
                declared=declared_silence_ranges,
                block_code="SILENCE_TOO_LONG",
                warn_code="SILENCE_DETECTED",
                declared_code="SILENCE_DECLARED",
            )
        )

    # -- loudness -----------------------------------------------------------
    measurement = loudness
    if measurement is None and measure_loudness_if_missing and audio_streams:
        if shutil.which(ffmpeg_bin) is not None:
            measurement = measure_loudness(path, ffmpeg_bin=ffmpeg_bin, timeout=timeout)
    checks.append(_loudness_check(measurement))

    # Planner pre-declaration annotation (Round 4): annotation ONLY —
    # it never changes a verdict, a reason code or a threshold; see
    # .declarations.  With no declared scenes every black/freeze range
    # is simply marked declared=False.
    return QCReport(
        media_path=str(path),
        checks=annotate_qc_checks(
            tuple(checks), declarations_from_scenes(declared_scenes)
        ),
        probe=probe,
        detections=detections,
        loudness=measurement,
    )
