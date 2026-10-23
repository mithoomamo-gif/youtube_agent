"""Independent loudness QC for the finished file.

The QC gate *re-measures* the finished (muxed) file with a fresh loudnorm
measurement invocation.  It never reuses the mastering passes' stored
values: a stored measurement is a claim, the re-measurement is the
verification (roadmap §18).  The same :func:`measure_loudness` function is
used standalone and by the gate, but it always runs its own FFmpeg pass on
the file it is given.

Also here: :func:`detect_silence_gaps`, the ``silencedetect``-based helper
that flags narration gaps > 3 s — in a fully automatic pipeline those are
almost always a failed TTS chunk, not an artistic pause (roadmap §10/§18).
"""

from __future__ import annotations

import re
from pathlib import Path

from ._ffmpeg import (
    loudnorm_value,
    parse_loudnorm_json,
    probe_channels,
    probe_duration,
    require_file,
    run_ffmpeg_capture,
)
from .models import (
    DEFAULT_TARGETS,
    LoudnessMeasurement,
    LoudnessTargets,
    QCResult,
    SilenceGap,
)

#: Narration silence longer than this (seconds) is flagged as suspicious.
SILENCE_GAP_THRESHOLD_S = 3.0
#: silencedetect noise floor; narration sits far above this, room tone /
#: digital silence far below it.
SILENCE_NOISE_DB = -50.0


def measure_loudness(
    path: Path | str,
    *,
    targets: LoudnessTargets = DEFAULT_TARGETS,
) -> LoudnessMeasurement:
    """Measure a file's integrated loudness / true peak / LRA, fresh.

    Runs a dedicated measure-only loudnorm pass (``print_format=json``)
    against ``path`` — any container FFmpeg can decode, WAV or muxed MP4.
    Mono inputs are measured with ``dual_mono=true`` so a mono stem's
    loudness is comparable to the stereo programme value.
    """
    src = require_file(Path(path), "measurement")
    dual_mono = ":dual_mono=true" if probe_channels(src) == 1 else ""
    stderr = run_ffmpeg_capture([
        "-i", str(src), "-vn",
        "-af", (
            f"loudnorm=I={targets.integrated_lufs:g}:TP={targets.true_peak_dbtp:g}"
            f":LRA={targets.lra:g}:print_format=json{dual_mono}"
        ),
        "-f", "null", "-",
    ])
    raw = parse_loudnorm_json(stderr)
    return LoudnessMeasurement(
        integrated_lufs=loudnorm_value(raw, "input_i"),
        true_peak_dbtp=loudnorm_value(raw, "input_tp"),
        lra=loudnorm_value(raw, "input_lra"),
        threshold_lufs=loudnorm_value(raw, "input_thresh"),
        target_offset=_optional_float(raw.get("target_offset")),
        normalization_type=raw.get("normalization_type"),
        raw=raw,
    )


def _optional_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")


def detect_silence_gaps(
    path: Path | str,
    *,
    threshold_s: float = SILENCE_GAP_THRESHOLD_S,
    noise_db: float = SILENCE_NOISE_DB,
) -> list[SilenceGap]:
    """Return silence runs longer than ``threshold_s`` in ``path``.

    Uses ``silencedetect`` with a low internal minimum duration (0.5 s)
    and filters by ``threshold_s`` afterwards, so the reported start/end
    are the true gap boundaries rather than threshold-clipped ones.  A gap
    that runs to end-of-file is closed against the probed duration.
    """
    src = require_file(Path(path), "silence detection")
    stderr = run_ffmpeg_capture([
        "-i", str(src),
        "-af", f"silencedetect=noise={noise_db:g}dB:d=0.5",
        "-f", "null", "-",
    ])

    gaps: list[SilenceGap] = []
    pending_start: float | None = None
    for line in stderr.splitlines():
        start = _SILENCE_START_RE.search(line)
        if start:
            pending_start = float(start.group(1))
            continue
        end = _SILENCE_END_RE.search(line)
        if end and pending_start is not None:
            gap = SilenceGap(start_s=pending_start, end_s=float(end.group(1)))
            if gap.duration_s > threshold_s:
                gaps.append(gap)
            pending_start = None
    if pending_start is not None:
        gap = SilenceGap(start_s=pending_start, end_s=probe_duration(src))
        if gap.duration_s > threshold_s:
            gaps.append(gap)
    return gaps


def qc_check(
    path: Path | str,
    *,
    targets: LoudnessTargets = DEFAULT_TARGETS,
    check_silence: bool = True,
) -> QCResult:
    """Run the independent QC gate on a finished (muxed) file.

    PASS requires *both*: integrated loudness within
    ``target ± tolerance`` LUFS and true peak ≤ the target ceiling.
    Silence gaps > 3 s (when ``check_silence``) are attached as warnings —
    flagged as a suspected failed TTS chunk for the caller to route back
    to the voice stage, without conflating them into the loudness verdict.
    """
    src = require_file(Path(path), "QC")
    measurement = measure_loudness(src, targets=targets)

    low = targets.integrated_lufs - targets.tolerance_lufs
    high = targets.integrated_lufs + targets.tolerance_lufs
    loudness_ok = low <= measurement.integrated_lufs <= high
    peak_ok = measurement.true_peak_dbtp <= targets.true_peak_dbtp

    failures: list[str] = []
    if not loudness_ok:
        failures.append(
            f"integrated loudness {measurement.integrated_lufs:.2f} LUFS outside "
            f"{targets.integrated_lufs:g} ± {targets.tolerance_lufs:g} LUFS"
        )
    if not peak_ok:
        failures.append(
            f"true peak {measurement.true_peak_dbtp:.2f} dBTP exceeds "
            f"{targets.true_peak_dbtp:g} dBTP ceiling"
        )

    gaps: list[SilenceGap] = []
    warnings: list[str] = []
    if check_silence:
        gaps = detect_silence_gaps(src)
        for gap in gaps:
            warnings.append(
                f"silence gap {gap.duration_s:.1f}s at {gap.start_s:.1f}s "
                f"(> {SILENCE_GAP_THRESHOLD_S:g}s) — suspected failed TTS chunk"
            )

    return QCResult(
        path=src,
        measurement=measurement,
        passed=loudness_ok and peak_ok,
        loudness_ok=loudness_ok,
        peak_ok=peak_ok,
        silence_gaps=gaps,
        failures=failures,
        warnings=warnings,
        details={
            "target_integrated_lufs": targets.integrated_lufs,
            "tolerance_lufs": targets.tolerance_lufs,
            "true_peak_ceiling_dbtp": targets.true_peak_dbtp,
        },
    )
