"""Technical QC for the YouTube Agent pipeline (Agent 32).

Real-FFmpeg QC of the final rendered file: ffprobe contract checks, one
decode pass for black/freeze/silence detection, and independent loudness
re-measurement.  See :mod:`.technical`.
"""

from .technical import (
    BLACK_BLOCK_MIN_S,
    BLACK_WARN_MIN_S,
    FREEZE_BLOCK_MIN_S,
    FREEZE_WARN_MIN_S,
    LOUDNESS_TARGET_LUFS,
    SILENCE_BLOCK_MIN_S,
    SILENCE_WARN_MIN_S,
    CheckResult,
    LoudnessMeasurement,
    MediaContract,
    QCReport,
    TimeRange,
    measure_loudness,
    parse_blackdetect,
    parse_freezedetect,
    parse_loudnorm_json,
    parse_silencedetect,
    probe_media,
    run_technical_qc,
)
from .declarations import (
    SceneDeclaration,
    annotate_qc_checks,
    annotate_qc_report,
    declarations_from_scenes,
)
from .tokens import GateToken, Verdict, issue_token, verify_token_binding

__all__ = [
    "run_technical_qc",
    "SceneDeclaration",
    "annotate_qc_checks",
    "annotate_qc_report",
    "declarations_from_scenes",
    "QCReport",
    "CheckResult",
    "MediaContract",
    "LoudnessMeasurement",
    "TimeRange",
    "probe_media",
    "measure_loudness",
    "parse_blackdetect",
    "parse_freezedetect",
    "parse_silencedetect",
    "parse_loudnorm_json",
    "BLACK_WARN_MIN_S",
    "BLACK_BLOCK_MIN_S",
    "FREEZE_WARN_MIN_S",
    "FREEZE_BLOCK_MIN_S",
    "SILENCE_WARN_MIN_S",
    "SILENCE_BLOCK_MIN_S",
    "LOUDNESS_TARGET_LUFS",
    "GateToken",
    "Verdict",
    "issue_token",
    "verify_token_binding",
]
