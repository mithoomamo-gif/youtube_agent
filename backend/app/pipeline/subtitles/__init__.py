"""Subtitles worker (Agent 19) for the YouTube Agent desktop app.

Pipeline position: TTS alignment / faster-whisper timings + the original
script text in -> ``.srt`` sidecar out (never burned into the master by
default; burn-in cue *data* is available for the render module).

Public API
----------
* :class:`WhisperTranscriber` / :func:`transcribe` — faster-whisper with
  lazy import and an alignment-derived stub fallback (provenance marked
  ``"whisper"`` vs ``"alignment_stub"``).
* :func:`build_cues` / :func:`build_srt` / :func:`parse_srt` — SRT
  generation from word timings + script sentences.
* :func:`check_cue_offsets` — 200 ms offset gate against reference timings.
* :func:`script_match_ratio` / :func:`check_script_match` — script-match
  helper + profile check (Roman profiles require >= 98%).
* :func:`script_match_ratio_fair` — transliteration-aware fair metric;
  reported alongside the strict score in every production QC report
  (R4-005), each score named via :data:`STRICT_SCRIPT_MATCH_METRIC` /
  :data:`FAIR_SCRIPT_MATCH_METRIC`.  Report-only per
  :data:`SUBTITLE_SCRIPT_MATCH_GATE_POLICY`.
* :func:`build_subtitles` / :func:`write_srt_sidecar` /
  :func:`cues_for_burn_in` — service entry points.
"""

from .models import (
    ROMAN_PROFILES,
    SOURCE_ALIGNMENT,
    SOURCE_ALIGNMENT_STUB,
    SOURCE_WHISPER,
    CaptionsProfile,
    Cue,
    TranscriptSegment,
    WordTiming,
    coerce_word_timings,
    flatten_segment_words,
)
from .service import SubtitleResult, build_subtitles, cues_for_burn_in, write_srt_sidecar
from .srt_builder import (
    MAX_CHARS_PER_SECOND,
    MAX_CUE_CHARS,
    MAX_CUE_DURATION_S,
    MAX_LINE_CHARS,
    MAX_LINES_PER_CUE,
    MIN_CUE_DURATION_S,
    build_cues,
    build_srt,
    format_timestamp,
    parse_srt,
    parse_timestamp,
    render_srt,
    split_script_sentences,
    wrap_text,
)
from .validation import (
    FAIR_SCRIPT_MATCH_METRIC,
    MIN_SCRIPT_MATCH_BY_PROFILE,
    OFFSET_TOLERANCE_MS,
    ROMAN_MIN_SCRIPT_MATCH,
    STRICT_SCRIPT_MATCH_METRIC,
    SUBTITLE_SCRIPT_MATCH_GATE_POLICY,
    OffsetReport,
    OffsetViolation,
    ScriptMatchReport,
    character_script_ratio,
    check_cue_offsets,
    check_script_match,
    normalise_for_match,
    script_match_ratio,
    script_match_ratio_fair,
    transliterate_urdu_to_roman,
)
from .whisper_wrapper import (
    STUB_ENV_VAR,
    SubtitlesStubError,
    WhisperTranscriber,
    probe_duration_seconds,
    segments_from_alignment_words,
    segments_from_script_duration,
    stub_requested_by_env,
    transcribe,
)

__all__ = [
    # models
    "CaptionsProfile",
    "Cue",
    "ROMAN_PROFILES",
    "SOURCE_ALIGNMENT",
    "SOURCE_ALIGNMENT_STUB",
    "SOURCE_WHISPER",
    "TranscriptSegment",
    "WordTiming",
    "coerce_word_timings",
    "flatten_segment_words",
    # whisper wrapper
    "STUB_ENV_VAR",
    "SubtitlesStubError",
    "WhisperTranscriber",
    "probe_duration_seconds",
    "segments_from_alignment_words",
    "segments_from_script_duration",
    "stub_requested_by_env",
    "transcribe",
    # srt builder
    "MAX_CHARS_PER_SECOND",
    "MAX_CUE_CHARS",
    "MAX_CUE_DURATION_S",
    "MAX_LINE_CHARS",
    "MAX_LINES_PER_CUE",
    "MIN_CUE_DURATION_S",
    "build_cues",
    "build_srt",
    "format_timestamp",
    "parse_srt",
    "parse_timestamp",
    "render_srt",
    "split_script_sentences",
    "wrap_text",
    # validation
    "MIN_SCRIPT_MATCH_BY_PROFILE",
    "OFFSET_TOLERANCE_MS",
    "ROMAN_MIN_SCRIPT_MATCH",
    "STRICT_SCRIPT_MATCH_METRIC",
    "FAIR_SCRIPT_MATCH_METRIC",
    "SUBTITLE_SCRIPT_MATCH_GATE_POLICY",
    "OffsetReport",
    "OffsetViolation",
    "ScriptMatchReport",
    "character_script_ratio",
    "check_cue_offsets",
    "check_script_match",
    "normalise_for_match",
    "script_match_ratio",
    "script_match_ratio_fair",
    "transliterate_urdu_to_roman",
    # service
    "SubtitleResult",
    "build_subtitles",
    "cues_for_burn_in",
    "write_srt_sidecar",
]
