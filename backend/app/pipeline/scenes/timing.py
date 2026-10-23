"""Narration timing spine — measured durations are truth.

Roadmap §12 / Part 6.4, principle 1: *audio is the master clock*.
Scenes are planned from MEASURED narration timings, never raw text —
text estimates drift 15-30% against real TTS and every downstream
artefact (subtitles, text cues, music ducking) inherits that drift.

This module provides:

* :func:`probe_duration_s` — the ffprobe-based duration probe.
* :func:`build_timing_spine` — takes per-sentence TTS segments
  (measured durations and/or per-segment WAV files) plus optional
  word-alignment data and produces the authoritative, contiguous
  sentence -> (start, end) timing list.

Drift policy: when a segment carries both a WAV file and a duration
figure, the file is probed and the *probed* value wins — a stale or
estimated figure can never override a measurement.  A segment with
neither a file nor a measured duration is a hard error: this module
will not invent timings from text length.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field
from ...ffmpeg_resolution import ffprobe_executable

FFPROBE_TIMEOUT_S = 30.0
#: Tolerance when checking that alignment words sit inside their segment.
ALIGNMENT_TOLERANCE_S = 0.05


class ProbeError(RuntimeError):
    """ffprobe could not produce a trustworthy duration for a file."""


def probe_duration_s(path: Union[str, Path]) -> float:
    """Return the exact duration (seconds) of a media file via ffprobe.

    Uses ``ffprobe -show_entries format=duration`` (the same probe the
    render/QC stages use).  Raises :class:`ProbeError` on any failure —
    callers must never fall back to a text estimate.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise ProbeError(f"narration segment file does not exist: {file_path}")
    cmd = [
        ffprobe_executable(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(file_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_S
        )
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out probing {file_path}") from exc
    if result.returncode != 0:
        raise ProbeError(
            f"ffprobe failed for {file_path}: {result.stderr.strip()[:300]}"
        )
    try:
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProbeError(
            f"ffprobe returned no usable duration for {file_path}: "
            f"{result.stdout.strip()[:200]}"
        ) from exc
    if duration <= 0:
        raise ProbeError(f"ffprobe reported a non-positive duration for {file_path}")
    return duration


class WordAlignment(BaseModel):
    """One aligned word from forced alignment (e.g. faster-whisper).

    Times are *segment-local* when supplied on a segment input and are
    rebased onto the spine by :func:`build_timing_spine`.
    """

    model_config = ConfigDict(extra="forbid")

    word: str
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)

    def rebased(self, offset_s: float) -> "WordAlignment":
        return WordAlignment(
            word=self.word,
            start_s=self.start_s + offset_s,
            end_s=self.end_s + offset_s,
        )


class NarrationSegment(BaseModel):
    """One per-sentence TTS segment feeding the spine.

    ``audio_path`` (probed with ffprobe) is the preferred input;
    ``measured_duration_s`` is accepted when the TTS stage already
    measured the segment with ffprobe.  ``estimated_duration_s`` exists
    only so drift can be *reported* — it is never used as a timing.
    """

    model_config = ConfigDict(extra="forbid")

    sentence_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    audio_path: Optional[str] = None
    measured_duration_s: Optional[float] = Field(default=None, gt=0)
    estimated_duration_s: Optional[float] = Field(default=None, gt=0)
    #: Word alignment with segment-local timestamps (optional).
    words: list[WordAlignment] = Field(default_factory=list)


class SceneTiming(BaseModel):
    """One row of the timing spine: a sentence pinned to the timeline."""

    model_config = ConfigDict(extra="forbid")

    sentence_id: str
    text: str
    start_s: float
    end_s: float
    #: The measured duration this row was built from (== end - start
    #: unless an inter-sentence gap was inserted by the caller).
    measured_duration_s: float
    #: How the duration was obtained: "ffprobe" or "measured".
    duration_source: str
    #: measured - text-estimate, when an estimate was supplied.  Kept so
    #: drift is visible in reports instead of silently absorbed.
    drift_s: Optional[float] = None
    #: Word alignment rebased onto the spine timeline.
    words: list[WordAlignment] = Field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def start_ms(self) -> int:
        return round(self.start_s * 1000)

    @property
    def end_ms(self) -> int:
        return round(self.end_s * 1000)


class TimingSpine(BaseModel):
    """The authoritative timing spine for one narration."""

    model_config = ConfigDict(extra="forbid")

    entries: list[SceneTiming] = Field(min_length=1)
    total_duration_s: float

    def timing_for(self, sentence_id: str) -> Optional[SceneTiming]:
        for entry in self.entries:
            if entry.sentence_id == sentence_id:
                return entry
        return None


def _measure_segment(segment: NarrationSegment) -> tuple[float, str]:
    """Return (measured duration, source) — ffprobe always wins."""
    if segment.audio_path is not None:
        return probe_duration_s(segment.audio_path), "ffprobe"
    if segment.measured_duration_s is not None:
        return segment.measured_duration_s, "measured"
    raise ValueError(
        f"segment {segment.sentence_id!r} has neither an audio file to "
        f"probe nor a measured duration; refusing to estimate timing "
        f"from text (estimates drift 15-30% against real narration)"
    )


def build_timing_spine(
    segments: Sequence[NarrationSegment],
    *,
    start_offset_s: float = 0.0,
    gap_s: float = 0.0,
) -> TimingSpine:
    """Build the contiguous sentence -> (start, end) spine.

    Segments are laid end-to-end in the order given, starting at
    ``start_offset_s`` with ``gap_s`` of join silence between them
    (the Voice Bible's join pauses; default 0).  Each segment's span is
    exactly its *measured* duration.  Word alignment, when supplied
    with segment-local timestamps, is validated against the measured
    span and rebased onto the spine.
    """
    if not segments:
        raise ValueError("cannot build a timing spine from zero segments")
    if start_offset_s < 0 or gap_s < 0:
        raise ValueError("start_offset_s and gap_s must be >= 0")

    entries: list[SceneTiming] = []
    cursor = start_offset_s
    seen_ids: set[str] = set()
    for segment in segments:
        if segment.sentence_id in seen_ids:
            raise ValueError(f"duplicate sentence_id in spine: {segment.sentence_id!r}")
        seen_ids.add(segment.sentence_id)

        measured, source = _measure_segment(segment)
        start = cursor
        end = start + measured

        words: list[WordAlignment] = []
        for word in segment.words:
            if word.end_s > measured + ALIGNMENT_TOLERANCE_S:
                raise ValueError(
                    f"alignment word {word.word!r} ends at {word.end_s:.3f}s, "
                    f"outside segment {segment.sentence_id!r} measured span "
                    f"({measured:.3f}s) — alignment does not match the "
                    f"measured narration"
                )
            words.append(word.rebased(start))

        drift = (
            measured - segment.estimated_duration_s
            if segment.estimated_duration_s is not None
            else None
        )
        entries.append(
            SceneTiming(
                sentence_id=segment.sentence_id,
                text=segment.text,
                start_s=start,
                end_s=end,
                measured_duration_s=measured,
                duration_source=source,
                drift_s=drift,
                words=words,
            )
        )
        cursor = end + gap_s

    total = entries[-1].end_s - start_offset_s
    return TimingSpine(entries=entries, total_duration_s=total)


# Convenience alias matching the roadmap's vocabulary.
build_scene_timings = build_timing_spine
