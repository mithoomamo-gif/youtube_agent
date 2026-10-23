"""Style Lab — speech transcription & structure extraction.

This module analyses a competitor video's *speech*: what is said, how
fast, and how the narration is structured (hook, beats, calls to
action).  It deliberately analyses **structure, not content**: beat
summaries are structural labels (``"problem statement"``, ``"list
item"`` …) and any transcript evidence quoted in notes is capped at a
few words.  The full transcript is written to a sidecar file and only
*referenced* (``transcript_ref``) from the analysis dict, so the
Style Lab analysis JSON never embeds a competitor's script.

Output shape (the ``speech`` section of a Style Lab analysis)::

    {
      "transcript_languages": ["ur", ...],
      "wpm": 182.4,
      "hook": {"window_s": 30.0, "pattern": "question",
               "pattern_label": "question",
               "pattern_notes": "question: interrogative opening …"},
      "beats": [{"start_s": 0.0, "end_s": 42.5,
                 "summary": "problem statement"}, ...],
      "cta": {"present": True,
              "placements": [{"kind": "subscribe", "position": "end",
                              "timestamp_s": 171.2}, ...]},
      "transcript_ref": "/…/video.transcript.json",
    }

Transcription
-------------
:func:`transcribe` extracts the audio track with FFmpeg (mono 16 kHz
WAV) and transcribes it through the project's existing provider path —
:func:`app.providers.transcription.create_transcriber`, the same
factory the E2E assemble stage uses for subtitles, with the same
project default model size (``base``, overridable via the
``YTA_WHISPER_MODEL_SIZE`` env var, mirroring
``app.integration.assemble_stage``).  Word and segment timestamps are
kept throughout.

FAIL-LOUD rule: if the real faster-whisper path is unavailable, the
pipeline wrapper would normally fall back to an alignment *stub* —
but that stub needs a known script, which a competitor video does not
have, and a fabricated transcript would poison the whole analysis.
:func:`transcribe` therefore raises :class:`SpeechTranscriptionError`
whenever the returned segments are not genuine ``whisper`` provenance.

Language honesty: faster-whisper reports ONE detected language per
transcription call — the dominant language as it sees it.  Real
faceless videos routinely mix languages (e.g. Urdu narration with
English terms); Whisper does not do per-segment language ID, and
minority-language words typically come back transcribed (often
imperfectly) in the dominant language's script.  ``transcript_languages``
therefore lists exactly what Whisper reported per segment (in practice
one code), and :func:`transcribe` adds a ``language_note`` saying so
instead of pretending a mixed-language breakdown exists.

Structure heuristics
--------------------
Everything in :func:`analyse_structure` is deterministic (regex /
keyword / sentence-shape rules, no model calls), so results are
reproducible and unit-testable on synthetic segment lists.  The exact
rules and their priority orders are documented on each helper below;
when the hook rules do not fire, the pattern is reported as
``"uncertain"`` (the shared schema's first-class no-claim value) — a
label is never forced.  Consistent with the schema's copyright rule,
``pattern_notes`` and beat summaries are structural descriptions
only: they never quote the transcript.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..ffmpeg_resolution import ffmpeg_executable

__all__ = [
    "DEFAULT_MODEL_SIZE",
    "HOOK_WINDOW_S",
    "SpeechTranscriptionError",
    "analyse_speech",
    "analyse_structure",
    "analyse_video_speech",
    "extract_audio",
    "transcribe",
]

#: Project default Whisper model size.  Mirrors
#: ``app.integration.assemble_stage.DEFAULT_WHISPER_MODEL_SIZE``
#: (``base`` — measured best small model for Urdu in Round 3) and its
#: ``YTA_WHISPER_MODEL_SIZE`` env override.
MODEL_SIZE_ENV_VAR = "YTA_WHISPER_MODEL_SIZE"
DEFAULT_MODEL_SIZE = "base"

#: The hook is read from the first 30 s of speech (roadmap hook window
#: is 15–30 s; 30 is used so a slightly late hook still counts).
HOOK_WINDOW_S = 30.0

#: Beat segmentation thresholds (see :func:`_segment_beats`).
PAUSE_GAP_S = 1.5
MAX_BEAT_S = 90.0
MIN_BEAT_S = 3.0

#: CTA placement zones, as fractions of the total duration.
CTA_START_FRACTION = 0.20
CTA_END_FRACTION = 0.80

_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


class SpeechTranscriptionError(RuntimeError):
    """Real transcription could not be produced (never silently stubbed)."""


# ---------------------------------------------------------------------------
# Normalisation — accept TranscriptSegment objects, dicts, or duck-typed rows
# ---------------------------------------------------------------------------


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


def _normalise_segments(segments: Any) -> list[dict[str, Any]]:
    """Coerce heterogeneous segment input into plain dicts.

    Accepts a list of ``TranscriptSegment``-like objects / dicts with
    ``start``/``start_s`` and ``end``/``end_s`` + ``text``, or the whole
    :func:`transcribe` result dict (its ``segments`` value is used).
    Word entries, when present, are reduced to their text + timings.
    """
    if isinstance(segments, dict) and "segments" in segments:
        segments = segments["segments"]
    out: list[dict[str, Any]] = []
    for item in segments or []:
        if item is None:
            continue
        start = float(_get(item, "start", "start_s", default=0.0))
        end = float(_get(item, "end", "end_s", default=start))
        words: list[dict[str, Any]] = []
        for w in _get(item, "words", default=()) or ():
            wtext = _get(w, "word", "text", default="")
            if isinstance(w, str):
                wtext = w
            if not str(wtext).strip():
                continue
            wstart = float(_get(w, "start", "start_s", default=start))
            wend = float(_get(w, "end", "end_s", default=wstart))
            words.append(
                {"word": str(wtext), "start": wstart, "end": max(wstart, wend)}
            )
        text = str(_get(item, "text", default="") or "").strip()
        if not text and words:
            text = " ".join(w["word"] for w in words).strip()
        out.append(
            {
                "start": start,
                "end": max(start, end),
                "text": text,
                "words": words,
                "language": _get(item, "language", default=None),
                "source": _get(item, "source", default=None),
            }
        )
    out.sort(key=lambda s: (s["start"], s["end"]))
    return out


def _word_count(seg: dict[str, Any]) -> int:
    if seg["text"]:
        return len(seg["text"].split())
    return len(seg["words"])


def _tokens(text: str) -> list[str]:
    """Whitespace tokens, punctuation-stripped, lower-cased (latin)."""
    strip_chars = " \t\r\n.,!?;:\"'()[]{}<>«»“”‘’…۔؟،-—"
    return [t.strip(strip_chars).lower() for t in text.split() if t.strip(strip_chars)]


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?۔؟])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s and s.strip()]


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


def extract_audio(video_path: str | Path, audio_path: str | Path) -> Path:
    """Extract the audio track of ``video_path`` as mono 16 kHz WAV.

    This is the format faster-whisper is happiest with and the same
    normalisation the subtitles path effectively relies on.
    """
    src, dst = Path(video_path), Path(audio_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            ffmpeg_executable(), "-y", "-v", "error",
            "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(dst),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0 or not dst.is_file():
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise SpeechTranscriptionError(
            f"ffmpeg audio extraction failed for {src}: {' | '.join(tail)}"
        )
    return dst


def _srt_timestamp(seconds: float) -> str:
    ms_total = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _render_srt(segments: Sequence[dict[str, Any]]) -> str:
    blocks = []
    for i, seg in enumerate(segments, 1):
        if not seg["text"]:
            continue
        blocks.append(
            f"{i}\n{_srt_timestamp(seg['start'])} --> "
            f"{_srt_timestamp(seg['end'])}\n{seg['text']}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def transcribe(
    video_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    model_size: str | None = None,
    language: str | None = None,
    transcriber: Any = None,
) -> dict[str, Any]:
    """Transcribe a video's speech via the project's faster-whisper path.

    Parameters
    ----------
    video_path:
        Video (or audio) file to transcribe.
    output_dir:
        Where the sidecars land; defaults to the video's own directory.
        The transcript JSON (``<stem>.transcript.json``), an SRT
        rendering (``<stem>.transcript.srt``) and the extracted WAV are
        written there.  Existing sidecars for the same stem are reused
        only in the sense that they are overwritten deterministically.
    model_size:
        faster-whisper model; defaults to the project default
        (``YTA_WHISPER_MODEL_SIZE`` env or ``base``).
    language:
        Force a language code; by default Whisper detects it.
    transcriber:
        Injection seam for tests — a pre-built transcriber object with
        the ``WhisperTranscriber`` interface.  Production callers leave
        this ``None`` and get a real one from
        :func:`app.providers.transcription.create_transcriber`.

    Returns a dict with the normalised ``segments`` (word timestamps
    kept), ``transcript_languages``, ``transcript_ref`` (path of the
    JSON sidecar — the analysis dict references this instead of
    embedding the transcript), ``srt_ref``, ``duration_s``,
    ``provenance`` and a ``language_note`` (see module docstring).
    Raises :class:`SpeechTranscriptionError` if genuine whisper output
    cannot be produced — the alignment stub is never passed off as a
    competitor transcript.
    """
    src = Path(video_path)
    if not src.is_file():
        raise FileNotFoundError(f"video file not found: {src}")
    out_dir = Path(output_dir) if output_dir is not None else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Reuse the subtitles wrapper's prober (WAV header / ffprobe).
    from app.pipeline.subtitles.whisper_wrapper import probe_duration_seconds

    probed = probe_duration_seconds(src)

    audio_path = out_dir / f"{src.stem}.audio.wav"
    if src.suffix.lower() in _AUDIO_SUFFIXES and src.resolve() == audio_path.resolve():
        extracted = src  # already the normalised WAV we would produce
    else:
        extracted = extract_audio(src, audio_path)

    resolved_model = (
        (model_size or "").strip()
        or (os.environ.get(MODEL_SIZE_ENV_VAR) or "").strip()
        or DEFAULT_MODEL_SIZE
    )
    if transcriber is None:
        from app.providers.transcription import create_transcriber

        # stub_mode=False is forced by the factory: the real model is
        # always attempted; a competitor video has no script, so the
        # wrapper's alignment fallback cannot (and must not) run.
        transcriber = create_transcriber(resolved_model)

    raw_segments = transcriber.transcribe(extracted, language=language)
    segments = _normalise_segments(list(raw_segments or []))
    provenance = getattr(transcriber, "last_provenance", None)
    last_error = getattr(transcriber, "last_error", None)
    if (
        not segments
        or provenance == "alignment_stub"
        or any(s.get("source") == "alignment_stub" for s in segments)
    ):
        raise SpeechTranscriptionError(
            "real faster-whisper transcription unavailable for "
            f"{src.name} (provenance={provenance!r}); refusing to "
            f"analyse a stub transcript. Underlying error: {last_error}"
        )

    # Whisper reports one detected language for the whole call; segments
    # carry it (the wrapper stamps it when detection ran).  Collect in
    # first-seen order, deduped.
    languages: list[str] = []
    for seg in segments:
        lang = seg.get("language")
        if lang and str(lang) not in languages:
            languages.append(str(lang))

    duration_s = float(probed) if probed else (
        segments[-1]["end"] if segments else 0.0
    )

    sidecar = {
        "version": 1,
        "kind": "stylelab_transcript",
        "source_video": str(src),
        "audio_ref": str(extracted),
        "model_size": resolved_model,
        "provenance": provenance or "whisper",
        "duration_s": round(duration_s, 3),
        "transcript_languages": languages,
        "segments": [
            {
                "start": round(s["start"], 3),
                "end": round(s["end"], 3),
                "text": s["text"],
                "language": s.get("language"),
                "words": [
                    {
                        "word": w["word"],
                        "start": round(w["start"], 3),
                        "end": round(w["end"], 3),
                    }
                    for w in s["words"]
                ],
            }
            for s in segments
        ],
    }
    transcript_ref = out_dir / f"{src.stem}.transcript.json"
    transcript_ref.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    srt_ref = out_dir / f"{src.stem}.transcript.srt"
    srt_ref.write_text(_render_srt(segments), encoding="utf-8")

    return {
        "video_path": str(src),
        "audio_path": str(extracted),
        "duration_s": round(duration_s, 3),
        "model_size": resolved_model,
        "provenance": provenance or "whisper",
        "transcript_languages": languages,
        "language_note": (
            "Whisper reports a single detected (dominant) language per "
            "transcription; mixed-language speech is not broken down "
            "per segment, and minority-language words are transcribed "
            "in the dominant language's script, often imperfectly."
        ),
        "transcript_ref": str(transcript_ref),
        "srt_ref": str(srt_ref),
        "segment_count": len(segments),
        "word_count": sum(_word_count(s) for s in segments),
        "segments": sidecar["segments"],
    }


# ---------------------------------------------------------------------------
# WPM
# ---------------------------------------------------------------------------


def _speech_wpm(segments: Sequence[dict[str, Any]], duration_s: float) -> float:
    """Words per minute over *speech* time.

    Speech time is the sum of segment durations (pauses / music beds
    between segments are excluded); if no segment carries timing, the
    total ``duration_s`` is the fallback denominator.
    """
    words = sum(_word_count(s) for s in segments)
    speech_s = sum(max(0.0, s["end"] - s["start"]) for s in segments)
    denominator = speech_s if speech_s > 0 else float(duration_s or 0.0)
    if words == 0 or denominator <= 0:
        return 0.0
    return round(words / (denominator / 60.0), 1)


# ---------------------------------------------------------------------------
# Hook detection (first HOOK_WINDOW_S seconds)
# ---------------------------------------------------------------------------

# Pattern priority is deliberate and deterministic — the FIRST matching
# rule wins, in this order:
#   1. preview      — the speaker verbally previews later content
#                     ("in this video we will …", "watch till the end",
#                     "aaj hum … dekhenge", "اس ویڈیو میں …").
#   2. question     — an interrogative sentence (final "?" / "؟", or a
#                     sentence opening with an interrogative word in
#                     English or Roman Urdu).
#   3. bold_claim   — superlatives / absolutes / secrecy & shock
#                     vocabulary / quantified claims ("deepest",
#                     "nobody knows", "secret", "raaz", "100%").
#   4. direct_address — second-person address or a direct greeting
#                     ("you", "aap", "آپ", "welcome back, friends").
# Nothing matched -> "unclear" (never force a label).

_PREVIEW_PHRASES = (
    "in this video", "in today's video", "in todays video",
    "today we will", "today i will", "we will explore", "we will discover",
    "we will see", "you will see", "you will learn", "you will discover",
    "watch till the end", "watch to the end", "by the end of this video",
    "coming up", "stay tuned",
    "is video mein", "is video me", "aaj hum", "aaj ki video",
    "aaj aap dekhenge", "aaj ham dekhenge",
    "اس ویڈیو میں", "اس ویڈیو میں ہم", "آج ہم", "آخر تک دیکھیں",
)

_INTERROGATIVE_STARTERS = {
    # English
    "what", "why", "how", "who", "whom", "when", "where", "which",
    "can", "could", "do", "does", "did", "is", "are", "was", "were",
    "have", "has", "would", "will",
    # Roman Urdu
    "kya", "kyun", "kyoon", "kaise", "kon", "kaun", "kab", "kahan",
    "kitne", "kitna", "kis", "kisko",
    # Urdu script (token comparison after whitespace split)
    "کیا", "کیوں", "کیسے", "کون", "کب", "کہاں", "کتنے",
}

_BOLD_CLAIM_RE = re.compile(
    r"\b(biggest|deepest|largest|greatest|darkest|deadliest|scariest|"
    r"strangest|strongest|coldest|hottest|oldest|fastest|richest)\b"
    r"|\b(never|always|nobody knows|no one knows|no one has|secret|"
    r"shocking|unbelievable|incredible|impossible|hidden truth)\b"
    r"|\b\d+(?:\.\d+)?\s?%"
    r"|\b(raaz|khatarnak|hairan)\b",
    re.IGNORECASE,
)
_BOLD_CLAIM_PHRASES = ("خطرناک", "راز", "حیران کن", "کبھی نہیں")

_SECOND_PERSON_TOKENS = {
    "you", "your", "yours", "yourself",
    "aap", "tum", "aapko", "tumhe", "tumko", "aapke", "tumhare",
    "آپ", "تم",
}
_GREETING_PHRASES = (
    "welcome", "hello", "hey guys", "hey everyone", "assalam",
    "السلام علیکم", "namaste",
)


def _detect_hook(
    segments: Sequence[dict[str, Any]], duration_s: float
) -> dict[str, Any]:
    window_s = float(min(HOOK_WINDOW_S, duration_s)) if duration_s else HOOK_WINDOW_S
    if window_s <= 0:
        window_s = HOOK_WINDOW_S
    window_text = " ".join(
        s["text"] for s in segments if s["text"] and s["start"] < window_s
    ).strip()
    base = {"window_s": round(window_s, 2)}
    if not window_text:
        return {
            **base,
            "pattern": "uncertain",
            "pattern_label": "uncertain",
            "pattern_notes": "uncertain: no speech detected in the opening window",
            "confidence": 0.4,
        }

    lower = window_text.lower()
    sentences = _sentences(window_text)

    def _hit(pattern: str, notes: str, confidence: float) -> dict[str, Any]:
        # "pattern" and the schema's "pattern_label" carry the same
        # value; the engine adapter accepts either.  Notes are purely
        # structural — no transcript wording is ever quoted (schema
        # copyright rule).
        return {
            **base,
            "pattern": pattern,
            "pattern_label": pattern,
            "pattern_notes": notes,
            "confidence": confidence,
        }

    # 1. preview of later content
    for phrase in _PREVIEW_PHRASES:
        if phrase in lower or phrase in window_text:
            return _hit(
                "preview",
                "preview: speaker verbally previews later content "
                "in the opening window",
                0.75,
            )

    # 2. question (any sentence in the window, weighted to the opening)
    for sentence in sentences[:3]:
        tokens = _tokens(sentence)
        if (
            sentence.rstrip().endswith(("?", "؟"))
            or (tokens and tokens[0] in _INTERROGATIVE_STARTERS)
        ):
            return _hit(
                "question",
                "question: interrogative sentence shape in the "
                "opening window",
                0.8,
            )

    # 3. bold claim
    match = _BOLD_CLAIM_RE.search(window_text) or next(
        (p for p in _BOLD_CLAIM_PHRASES if p in window_text), None
    )
    if match:
        return _hit(
            "bold_claim",
            "bold_claim: superlative/absolute or quantified claim "
            "in the opening window",
            0.7,
        )

    # 4. direct address
    token_set = set(_tokens(window_text))
    if token_set & _SECOND_PERSON_TOKENS or any(
        g in lower or g in window_text for g in _GREETING_PHRASES
    ):
        return _hit(
            "direct_address",
            "direct_address: second-person address or greeting in "
            "the opening window",
            0.65,
        )

    return _hit(
        "uncertain",
        "uncertain: no preview/question/bold-claim/direct-address "
        "pattern matched the opening window",
        0.4,
    )


# ---------------------------------------------------------------------------
# CTA detection
# ---------------------------------------------------------------------------

# (kind, regex) — English + Roman Urdu + Urdu-script phrasing.  Regexes
# run on the raw segment text (case-insensitive for latin scripts).
_CTA_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("subscribe", re.compile(
        r"subscrib\w*|سبسکرائب", re.IGNORECASE)),
    ("like", re.compile(
        r"like\s+(this\s+)?(video|it)\b|like\s+the\s+video|hit\s+(the\s+)?like"
        r"|like\s+kar\w*|لائک\s*کریں|لائک", re.IGNORECASE)),
    ("comment", re.compile(
        r"comment\w*\s+(below|down|karo|karen|mein|me)\b"
        r"|let\s+me\s+know\s+in\s+the\s+comments?"
        r"|comments?\s+mein\s+(bata\w*|likh\w*|apni)"
        r"|in\s+the\s+comments?\s+(below|section)"
        r"|کمنٹ", re.IGNORECASE)),
    ("share", re.compile(
        r"share\s+(this\s+)?(video|it)\b|share\s+kar\w*|شیئر", re.IGNORECASE)),
    ("bell", re.compile(
        r"bell\s+icon|hit\s+the\s+bell|notifications?\s+on|ghanti\w*\s+daba\w*"
        r"|بیل\s*آئیکن", re.IGNORECASE)),
    ("link_in_description", re.compile(
        r"link\s+in\s+(the\s+)?(description|bio)"
        r"|description\s+mein\s+link|check\s+the\s+link"
        r"|link\s+(is\s+)?in\s+(the\s+)?description"
        r"|ڈسکرپشن\s+میں\s+لنک", re.IGNORECASE)),
    ("follow", re.compile(
        r"follow\s+(me|us|kar\w*)\b", re.IGNORECASE)),
)


def _cta_position(timestamp_s: float, duration_s: float) -> str:
    if not duration_s or duration_s <= 0:
        return "middle"
    frac = timestamp_s / duration_s
    if frac < CTA_START_FRACTION:
        return "start"
    if frac >= CTA_END_FRACTION:
        return "end"
    return "middle"


def _detect_cta(
    segments: Sequence[dict[str, Any]], duration_s: float
) -> dict[str, Any]:
    placements: list[dict[str, Any]] = []
    for seg in segments:
        if not seg["text"]:
            continue
        for kind, pattern in _CTA_RULES:
            if pattern.search(seg["text"]):
                ts = round(seg["start"], 2)
                placements.append(
                    {
                        "kind": kind,
                        "position": _cta_position(seg["start"], duration_s),
                        "timestamp_s": ts,
                        "start_s": ts,
                        # Exact-phrasing regex match — high but not
                        # certain (phrasing can be descriptive, not
                        # imperative), hence 0.8, not 1.0.
                        "confidence": 0.8,
                    }
                )
    placements.sort(key=lambda p: (p["timestamp_s"], p["kind"]))
    return {
        "present": bool(placements),
        "placements": placements,
        "confidence": 0.8 if placements else 0.5,
    }


# ---------------------------------------------------------------------------
# Beats
# ---------------------------------------------------------------------------

# A new beat starts at a segment boundary when ANY of these fires:
#   * pause gap before the segment >= PAUSE_GAP_S (speaker paused —
#     the natural chapter break in narrated videos), or
#   * the segment opens with a discourse/topic-shift marker
#     (see _TOPIC_SHIFT_*), including a list ordinal, or
#   * the current beat would exceed MAX_BEAT_S (long monologues are
#     split at the nearest segment boundary).
# Afterwards, beats shorter than MIN_BEAT_S are merged into the
# following beat (the final one merges backwards) so a one-line aside
# does not become its own "chapter".

_TOPIC_SHIFT_PREFIXES = (
    "moving on", "on the other hand", "let's move", "lets move",
    "now let", "iske baad", "uske baad", "chaliye ab", "ab hum",
    "ab ham", "agla", "agli", "dusra point", "dusri baat",
)
# NOTE: bare "ab" / "phir" / "اب" / "پھر" are deliberately NOT split
# tokens — in Roman-Urdu/Urdu narration they open ordinary sentences
# constantly, so splitting on them would shred every beat.  Only the
# explicit topic-shift phrases above ("ab hum …" = "now we …") split.
_TOPIC_SHIFT_TOKENS = {
    "now", "next", "another", "meanwhile", "finally",
    "firstly", "secondly", "thirdly",
    "اگلا",
}
_ORDINAL_START_RE = re.compile(
    r"^\s*(first|second|third|fourth|fifth|number\s+\w+|\d+[\.\)]|"
    r"pehla|pehli|dusra|dusri|teesra|teesri)\b",
    re.IGNORECASE,
)


def _starts_new_topic(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    if _ORDINAL_START_RE.match(stripped):
        return True
    for prefix in _TOPIC_SHIFT_PREFIXES:
        if lower.startswith(prefix):
            return True
    tokens = _tokens(stripped)
    return bool(tokens and tokens[0] in _TOPIC_SHIFT_TOKENS)


def _split_beat_spans(
    segments: Sequence[dict[str, Any]],
) -> list[tuple[float, float, list[dict[str, Any]]]]:
    spans: list[tuple[float, float, list[dict[str, Any]]]] = []
    current: list[dict[str, Any]] = []
    for seg in segments:
        if current:
            prev = current[-1]
            gap = seg["start"] - prev["end"]
            beat_start = current[0]["start"]
            if (
                gap >= PAUSE_GAP_S
                or _starts_new_topic(seg["text"])
                or (seg["end"] - beat_start > MAX_BEAT_S)
            ):
                spans.append((beat_start, prev["end"], current))
                current = []
        current.append(seg)
    if current:
        spans.append((current[0]["start"], current[-1]["end"], current))

    # Merge too-short beats into the following one (last -> previous).
    merged: list[tuple[float, float, list[dict[str, Any]]]] = []
    pending: Optional[tuple[float, float, list[dict[str, Any]]]] = None
    for span in spans:
        if pending is not None:
            span = (pending[0], span[1], pending[2] + span[2])
            pending = None
        if span[1] - span[0] < MIN_BEAT_S:
            pending = span
            continue
        merged.append(span)
    if pending is not None:
        if merged:
            prev = merged[-1]
            merged[-1] = (prev[0], pending[1], prev[2] + pending[2])
        else:
            merged.append(pending)
    return merged


# Beat summary labels — structural ONLY, chosen by the first matching
# rule in this priority order: CTA > conclusion > list item > example
# > problem statement > explanation > question > story > hook (first
# beat) > main content.
_SUMMARY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("conclusion / recap", re.compile(
        r"in conclusion|to sum up|finally,?\s+remember|in the end,?\s+we"
        r"|akhir mein|aakhir mein|so remember|خلاصہ", re.IGNORECASE)),
    ("list item", re.compile(
        r"^\s*(first|second|third|fourth|fifth|number\s+\w+|\d+[\.\)]|"
        r"pehla|pehli|dusra|dusri|teesra|teesri)\b"
        r"|another (fact|reason|point|thing)", re.IGNORECASE)),
    ("example", re.compile(
        r"for example|for instance|\bimagine\b|\bsuppose\b|misal|مثال"
        r"|jaise ke|jaisa ke", re.IGNORECASE)),
    ("problem statement", re.compile(
        r"\bproblem\b|\bissue\b|\bchallenge\b|\bdanger\b|struggle|masla|"
        r"مشکل|مسئلہ|خطرہ", re.IGNORECASE)),
    ("explanation", re.compile(
        r"\bbecause\b|the reason|this is why|that means|kyunki|kyunke|"
        r"is liye|اس لیے|کیونکہ", re.IGNORECASE)),
    ("question posed", re.compile(r"\?|؟")),
    ("anecdote / story", re.compile(
        r"once upon|\bstory\b|kahani|کہانی|years ago|saal pehle",
        re.IGNORECASE)),
    ("transition", re.compile(
        r"^\s*(now|next|moving on|meanwhile|ab|phir|اب|پھر)\b",
        re.IGNORECASE)),
)


def _summarise_beat(
    text: str, *, is_first: bool, has_cta: bool
) -> tuple[str, float]:
    """(structural label, confidence) for one beat.

    A rule-matched label carries 0.7; the fallback labels are weaker
    claims (0.4) — they say "nothing distinctive detected", which is
    itself only a heuristic reading.
    """
    if has_cta:
        return "call to action", 0.8
    for label, pattern in _SUMMARY_RULES:
        if pattern.search(text):
            return label, 0.7
    return ("opening hook" if is_first else "main content"), 0.4


def _segment_beats(
    segments: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    beats: list[dict[str, Any]] = []
    spans = _split_beat_spans(segments)
    cta_kinds = [p for p in _CTA_RULES]
    for idx, (start, end, segs) in enumerate(spans):
        text = " ".join(s["text"] for s in segs if s["text"])
        has_cta = any(p.search(text) for _, p in cta_kinds) if text else False
        summary, confidence = _summarise_beat(
            text, is_first=(idx == 0), has_cta=has_cta
        )
        beats.append(
            {
                "start_s": round(start, 2),
                "end_s": round(end, 2),
                "summary": summary,
                "confidence": confidence,
            }
        )
    return beats


# ---------------------------------------------------------------------------
# Public analysis entry points
# ---------------------------------------------------------------------------


def analyse_structure(
    segments: Any, duration_s: float
) -> dict[str, Any]:
    """Derive WPM, hook, beats and CTA from transcription segments.

    ``segments`` is a list of ``TranscriptSegment``-like objects or
    dicts (or the :func:`transcribe` result dict); ``duration_s`` is
    the video's total duration.  Returns the structural part of the
    ``speech`` section: ``{wpm, hook, beats, cta}``.  All rules are
    deterministic — see the helper docstrings above.  Summaries and
    notes describe structure only and never quote the transcript.
    """
    normalised = _normalise_segments(segments)
    duration = float(duration_s or 0.0)
    if duration <= 0 and normalised:
        duration = normalised[-1]["end"]
    return {
        "wpm": _speech_wpm(normalised, duration),
        "hook": _detect_hook(normalised, duration),
        "beats": _segment_beats(normalised),
        "cta": _detect_cta(normalised, duration),
    }


def analyse_video_speech(
    video_path: str | Path, **transcribe_kwargs: Any
) -> dict[str, Any]:
    """Full ``speech`` section for one video: transcribe + analyse.

    The returned dict matches the Style Lab schema exactly
    (``transcript_languages``, ``wpm``, ``hook``, ``beats``, ``cta``,
    ``transcript_ref``) plus provenance/duration extras; the transcript
    itself lives only in the sidecar referenced by ``transcript_ref``.
    """
    result = transcribe(video_path, **transcribe_kwargs)
    structure = analyse_structure(result["segments"], result["duration_s"])
    return {
        "transcript_languages": result["transcript_languages"],
        "language_note": result["language_note"],
        # Whisper's language probability is not propagated by the
        # subtitles wrapper, so no confidence is claimed for it.
        "language_confidence": 0.0,
        "wpm": structure["wpm"],
        # WPM is a direct count over measured word timings.
        "wpm_confidence": 0.9 if result["word_count"] else 0.0,
        "hook": structure["hook"],
        "beats": structure["beats"],
        "cta": structure["cta"],
        "transcript_ref": result["transcript_ref"],
        "srt_ref": result["srt_ref"],
        "duration_s": result["duration_s"],
        "model_size": result["model_size"],
        "provenance": result["provenance"],
        "confidence": 0.7,
    }


def analyse_speech(
    video_path: str | Path,
    scenes: Any = None,
    **transcribe_kwargs: Any,
) -> dict[str, Any]:
    """Schema-contract entry point: ``analyse_speech(video, scenes)``.

    ``scenes`` is accepted for signature compatibility with the other
    Style Lab sub-analyses (and the engine's flexible caller); speech
    structure is derived from the transcript alone, so it is not used.
    Returns the same dict as :func:`analyse_video_speech`, which the
    engine converts into ``schema.SpeechAnalysis``.
    """
    return analyse_video_speech(video_path, **transcribe_kwargs)
