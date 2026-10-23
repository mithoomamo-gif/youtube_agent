"""Subtitle service: orchestration + sidecar output.

Produces the ``.srt`` **sidecar only** — subtitles are never burned into
the master by default (roadmap D5).  For the opt-in burn-in variant, the
render module consumes :func:`cues_for_burn_in`, which returns plain,
JSON-serialisable cue data (no rendering happens here).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

from .models import (
    SOURCE_ALIGNMENT,
    SOURCE_ALIGNMENT_STUB,
    SOURCE_WHISPER,
    CaptionsProfile,
    Cue,
    TranscriptSegment,
    flatten_segment_words,
)
from .srt_builder import build_cues, render_srt, split_script_sentences
from .validation import ScriptMatchReport, check_script_match


@dataclass(frozen=True)
class SubtitleResult:
    """Everything a subtitle build produced, profile included."""

    cues: tuple[Cue, ...]
    srt_text: str
    profile: CaptionsProfile
    language: Optional[str] = None
    #: Provenance of the timings: "alignment" | "whisper" | "alignment_stub".
    source: str = SOURCE_ALIGNMENT
    script_match: Optional[ScriptMatchReport] = None
    warnings: tuple[str, ...] = ()

    @property
    def cue_count(self) -> int:
        return len(self.cues)

    @property
    def script_match_ratio(self) -> Optional[float]:
        return self.script_match.ratio if self.script_match else None

    @property
    def script_match_ratio_fair(self) -> Optional[float]:
        """Transliteration-aware fair score (report-only, R4-005)."""
        return self.script_match.ratio_fair if self.script_match else None

    @property
    def script_match_metrics(self) -> Optional[dict[str, Any]]:
        """Both subtitle scores with their metric names (R4-005)."""
        return self.script_match.metrics_dict() if self.script_match else None

    @property
    def script_match_passed(self) -> Optional[bool]:
        return self.script_match.passed if self.script_match else None

    @property
    def metadata(self) -> dict[str, Any]:
        """JSON-serialisable build metadata (what a project record stores).

        Carries BOTH subtitle script-match scores (R4-005): the strict
        ``script_match_ratio`` and the transliteration-aware
        ``script_match_ratio_fair``, plus ``script_match_metrics`` which
        pairs each score with its metric name so neither is a bare,
        unlabelled number.  ``script_match_passed`` remains decided by
        the strict score only.
        """
        return {
            "profile": self.profile.value,
            "language": self.language,
            "source": self.source,
            "cue_count": self.cue_count,
            "script_match_ratio": self.script_match_ratio,
            "script_match_ratio_fair": self.script_match_ratio_fair,
            "script_match_metrics": self.script_match_metrics,
            "script_match_passed": self.script_match_passed,
            "warnings": list(self.warnings),
        }


def build_subtitles(
    word_timings: Optional[Iterable[Any]] = None,
    *,
    script_text: Optional[str] = None,
    script_sentences: Optional[Sequence[str]] = None,
    transcription_segments: Optional[Sequence[TranscriptSegment]] = None,
    profile: CaptionsProfile | str = CaptionsProfile.ENGLISH,
    language: Optional[str] = None,
    source: Optional[str] = None,
) -> SubtitleResult:
    """Build cues + SRT text from word timings and the original script.

    Timing source, in priority order: ``transcription_segments`` (whisper
    or stub output), else ``word_timings`` (TTS alignment — the happy
    path).  Wording always comes from ``script_text`` / ``script_sentences``
    when their token count matches the timings; the respelt TTS text is
    never an input here.
    """
    profile = CaptionsProfile.coerce(profile)
    warnings: list[str] = []

    if transcription_segments is not None:
        words = flatten_segment_words(transcription_segments)
        if source is None:
            sources = {segment.source for segment in transcription_segments}
            if sources == {SOURCE_ALIGNMENT_STUB}:
                source = SOURCE_ALIGNMENT_STUB
            elif SOURCE_WHISPER in sources:
                source = SOURCE_WHISPER
            elif sources:
                source = next(iter(sources))
            else:
                source = SOURCE_ALIGNMENT
        if language is None:
            for segment in transcription_segments:
                if segment.language:
                    language = segment.language
                    break
    else:
        words = list(word_timings) if word_timings is not None else []
        if source is None:
            source = SOURCE_ALIGNMENT

    sentences: list[str]
    if script_sentences is not None:
        sentences = [str(s) for s in script_sentences]
    elif script_text:
        sentences = split_script_sentences(script_text)
    else:
        sentences = []
        warnings.append("no script text supplied; cue wording comes from timings")

    full_script = script_text or " ".join(sentences)
    cues = build_cues(words, sentences)
    srt_text = render_srt(cues)

    script_match: Optional[ScriptMatchReport] = None
    if full_script.strip() and cues:
        caption_text = " ".join(cue.text for cue in cues)
        script_match = check_script_match(full_script, caption_text, profile)
        if not script_match.passed:
            # WARN context (R4-005): the fair score rides along so a
            # cross-script 0.0 is not misread as total transcription
            # failure.  It never changes `passed` — the strict metric
            # alone decides (SUBTITLE_SCRIPT_MATCH_GATE_POLICY).
            fair_note = ""
            if script_match.ratio_fair is not None:
                fair_note = (
                    f"; fair {script_match.fair_metric}="
                    f"{script_match.ratio_fair:.3f} (report-only context)"
                )
            warnings.append(
                f"script-match {script_match.ratio:.3f} "
                f"({script_match.strict_metric}) below "
                f"{script_match.threshold:.2f} threshold for profile "
                f"{profile.value}{fair_note}"
            )
    if source == SOURCE_ALIGNMENT_STUB:
        warnings.append(
            "timings are alignment-derived (stub mode), not a whisper pass"
        )

    return SubtitleResult(
        cues=tuple(cues),
        srt_text=srt_text,
        profile=profile,
        language=language,
        source=source,
        script_match=script_match,
        warnings=tuple(warnings),
    )


def write_srt_sidecar(
    result_or_cues: Union[SubtitleResult, Sequence[Cue]],
    output_path: str | Path,
) -> Path:
    """Write the ``.srt`` sidecar file and return its path.

    This is the only file this package writes for the master: no video is
    touched.  A path without an ``.srt`` suffix gets one appended so the
    artefact type is never ambiguous.
    """
    if isinstance(result_or_cues, SubtitleResult):
        srt_text = result_or_cues.srt_text
    else:
        srt_text = render_srt(list(result_or_cues))
    path = Path(output_path)
    if path.suffix.lower() != ".srt":
        path = Path(str(path) + ".srt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(srt_text, encoding="utf-8")
    return path


def cues_for_burn_in(
    result_or_cues: Union[SubtitleResult, Sequence[Cue]],
) -> list[dict[str, Any]]:
    """Plain cue data for the render module's optional burn-in variant.

    Returns JSON-serialisable dicts only — segment-level ASS burn-in is the
    render module's job; nothing is rendered here.
    """
    if isinstance(result_or_cues, SubtitleResult):
        cues = result_or_cues.cues
        profile: Optional[str] = result_or_cues.profile.value
        language = result_or_cues.language
    else:
        cues = tuple(result_or_cues)
        profile = None
        language = None
    return [
        {
            "index": cue.index,
            "start": cue.start,
            "end": cue.end,
            "duration": cue.duration,
            "text": cue.text,
            "lines": list(cue.lines),
            "profile": profile,
            "language": language,
        }
        for cue in cues
    ]
