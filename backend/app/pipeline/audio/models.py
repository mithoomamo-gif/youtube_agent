"""Shared data types for the audio pipeline (worker 3 — AUDIO PIPELINE).

All interfaces in this package are file-in / file-out: functions take
``pathlib.Path`` inputs and produce a WAV (or measure an existing media
file, e.g. the final muxed MP4).  Results are returned as the small frozen
dataclasses defined here so callers (orchestrator / QC gates) never have to
parse FFmpeg stderr themselves.

Spec sources:
- ``roadmap-sections/18_editing_render.md`` — audio chain, ducking,
  two-pass loudnorm, ``amix normalize=0``, QC re-measurement gate.
- ``roadmap-sections/10_voice_workflow.md`` — loudness targets and the
  ">3 s narration silence = suspected failed TTS chunk" rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LoudnessTargets:
    """Programme loudness targets for the final master (YouTube delivery).

    YouTube normalises playback to ≈ −14 LUFS integrated with a −1 dBTP
    ceiling, so the master is delivered exactly at target: louder material
    is simply turned down, quieter material is *not* fully turned up.
    """

    integrated_lufs: float = -14.0
    true_peak_dbtp: float = -1.0
    lra: float = 11.0
    #: QC acceptance window for integrated loudness (± LUFS around target).
    tolerance_lufs: float = 1.0
    sample_rate: int = 48_000


#: Locked default targets used across the pipeline.
DEFAULT_TARGETS = LoudnessTargets()


@dataclass(frozen=True)
class LoudnessMeasurement:
    """One loudness measurement of one file (EBU R128 / loudnorm values)."""

    integrated_lufs: float
    true_peak_dbtp: float
    lra: float
    threshold_lufs: float
    #: Gain loudnorm would still apply to hit the target (pass-1 JSON only;
    #: ``None`` for a plain measurement where it is not meaningful).
    target_offset: float | None = None
    #: ``"linear"`` / ``"dynamic"`` when reported by loudnorm, else ``None``.
    normalization_type: str | None = None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SilenceGap:
    """A detected run of silence inside a narration / programme file."""

    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass
class MixOutcome:
    """Result of :func:`app.pipeline.audio.mix.mix_stems` (pre-master mix)."""

    output_path: Path
    #: True when sidechain ducking was used; False when the volume-envelope
    #: fallback path produced the mix.
    sidechain_used: bool
    details: dict[str, object] = field(default_factory=dict)


@dataclass
class MasterResult:
    """Result of :func:`app.pipeline.audio.mix.mix_and_master`."""

    output_path: Path
    #: Pass-1 (measurement) loudnorm values of the pre-master mix.
    premaster_measurement: LoudnessMeasurement
    #: Fresh, independent re-measurement of the finished master file.
    final_measurement: LoudnessMeasurement
    #: True when pass 2 was able to normalise with a single linear gain.
    linear_used: bool
    #: True when the mix stage used sidechain ducking (vs. envelope fallback).
    sidechain_used: bool
    details: dict[str, object] = field(default_factory=dict)


@dataclass
class QCResult:
    """Independent QC verdict for a finished (muxed) file.

    ``passed`` is decided *only* by the loudness gate: integrated loudness
    within ``target ± tolerance`` and true peak at or below the ceiling.
    Long silence gaps are surfaced in :attr:`silence_gaps` / :attr:`warnings`
    as a suspected failed TTS chunk; they are flagged, not folded into the
    loudness verdict, so the two failure modes stay distinguishable.
    """

    path: Path
    measurement: LoudnessMeasurement
    passed: bool
    loudness_ok: bool
    peak_ok: bool
    silence_gaps: list[SilenceGap] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, object] = field(default_factory=dict)
