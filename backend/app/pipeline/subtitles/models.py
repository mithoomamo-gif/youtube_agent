"""Shared data types for the Subtitles worker (Agent 19).

Design ground rules (Master Roadmap Part 6.7 / section 18):

* Subtitles are built from the **original script text** — never from the
  respelt / pronunciation text handed to TTS.  Timings come from TTS word
  boundaries / alignment on the happy path, or from faster-whisper
  reconciled against the script.
* The master video stays clean: this package produces an ``.srt`` sidecar
  only, plus plain cue data the render module may use for the optional
  per-channel burn-in variant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# Provenance markers
# ---------------------------------------------------------------------------

#: Timings/text came from a real faster-whisper transcription pass.
SOURCE_WHISPER = "whisper"
#: Timings came from TTS word-boundary / alignment data (the happy path).
SOURCE_ALIGNMENT = "alignment"
#: faster-whisper was unavailable, so the whisper wrapper fell back to
#: alignment-derived timings.  Always surfaced — never passed off as whisper.
SOURCE_ALIGNMENT_STUB = "alignment_stub"


class CaptionsProfile(str, Enum):
    """Captions language/script profile, carried through every result."""

    URDU_SCRIPT = "urdu_script"
    ROMAN_URDU = "roman_urdu"
    ENGLISH = "english"

    @classmethod
    def coerce(cls, value: "CaptionsProfile | str | None") -> "CaptionsProfile":
        if value is None:
            return cls.ENGLISH
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            raise ValueError(
                f"Unknown captions profile {value!r}; expected one of "
                f"{[p.value for p in cls]}"
            ) from None


#: Profiles whose captions are written in the Latin ("Roman") script.  These
#: require a >= 98% script-match against the original script (see
#: :mod:`.validation`).
ROMAN_PROFILES = frozenset({CaptionsProfile.ROMAN_URDU, CaptionsProfile.ENGLISH})


@dataclass(frozen=True)
class WordTiming:
    """One word with measured start/end times (seconds, audio timeline)."""

    word: str
    start: float
    end: float
    source: str = SOURCE_ALIGNMENT
    probability: Optional[float] = None

    @property
    def text(self) -> str:  # convenience alias (some providers call it "text")
        return self.word

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class TranscriptSegment:
    """A transcription segment with word-level timestamps."""

    text: str
    start: float
    end: float
    words: tuple[WordTiming, ...] = ()
    language: Optional[str] = None
    source: str = SOURCE_WHISPER

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class Cue:
    """One subtitle cue. ``lines`` is 1–2 display lines (<= 42 chars each)."""

    index: int
    start: float
    end: float
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        """Cue text as a single line (words joined with spaces)."""
        return " ".join(line.strip() for line in self.lines).strip()

    @property
    def srt_text(self) -> str:
        """Cue text exactly as written inside the SRT block."""
        return "\n".join(self.lines)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def chars_per_second(self) -> float:
        if self.duration <= 0:
            return float("inf")
        return len(self.text) / self.duration


# ---------------------------------------------------------------------------
# Coercion helpers — accept the shapes other pipeline stages actually emit
# ---------------------------------------------------------------------------


def _word_text(obj: Any) -> Optional[str]:
    for attr in ("word", "text"):
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    if isinstance(obj, dict):
        for key in ("word", "text"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def coerce_word_timings(items: Optional[Iterable[Any]]) -> list[WordTiming]:
    """Normalise heterogeneous timing data into ``list[WordTiming]``.

    Accepted per item:

    * :class:`WordTiming` (passed through),
    * ``(word, start, end)`` tuples/lists,
    * dicts with ``word``/``text`` + ``start`` + ``end`` keys,
    * objects with ``.word``/``.text``, ``.start``, ``.end`` attributes
      (e.g. faster-whisper ``Word`` objects),
    * segment-like objects with a ``.words`` collection (flattened),
    * cue-like objects with ``.lines`` + ``.start``/``.end`` (first token is
      timed at the cue start — used by the offset validator).
    """
    out: list[WordTiming] = []
    if not items:
        return out
    for item in items:
        if item is None:
            continue
        if isinstance(item, WordTiming):
            out.append(item)
            continue
        words = getattr(item, "words", None)
        if words:  # segment-like -> flatten
            out.extend(coerce_word_timings(words))
            continue
        if isinstance(item, (tuple, list)) and len(item) >= 3:
            word, start, end = item[0], float(item[1]), float(item[2])
            out.append(WordTiming(word=str(word), start=start, end=max(start, end)))
            continue
        if isinstance(item, dict):
            text = _word_text(item)
            if text is not None and "start" in item and "end" in item:
                start = float(item["start"])
                end = float(item["end"])
                out.append(
                    WordTiming(
                        word=text,
                        start=start,
                        end=max(start, end),
                        source=str(item.get("source", SOURCE_ALIGNMENT)),
                        probability=item.get("probability"),
                    )
                )
            continue
        # Cue-like object: .lines + .start/.end
        lines = getattr(item, "lines", None)
        if lines and hasattr(item, "start") and hasattr(item, "end"):
            text = " ".join(str(line).strip() for line in lines).strip()
            if text:
                start = float(item.start)  # type: ignore[attr-defined]
                end = float(item.end)  # type: ignore[attr-defined]
                out.append(
                    WordTiming(word=text.split()[0], start=start, end=max(start, end))
                )
            continue
        text = _word_text(item)
        if text is not None and hasattr(item, "start") and hasattr(item, "end"):
            start = float(item.start)  # type: ignore[attr-defined]
            end = float(item.end)  # type: ignore[attr-defined]
            out.append(
                WordTiming(
                    word=text,
                    start=start,
                    end=max(start, end),
                    source=str(getattr(item, "source", SOURCE_ALIGNMENT)),
                    probability=getattr(item, "probability", None),
                )
            )
    out.sort(key=lambda w: (w.start, w.end))
    return out


def flatten_segment_words(
    segments: Optional[Sequence[TranscriptSegment]],
) -> list[WordTiming]:
    """All word timings from transcription segments, in timeline order."""
    if not segments:
        return []
    words: list[WordTiming] = []
    for segment in segments:
        if segment.words:
            words.extend(segment.words)
        elif segment.text.strip():
            words.append(
                WordTiming(
                    word=segment.text.split()[0],
                    start=segment.start,
                    end=segment.end,
                    source=segment.source,
                )
            )
    words.sort(key=lambda w: (w.start, w.end))
    return words
