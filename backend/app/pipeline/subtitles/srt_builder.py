"""SRT generation from word timings + the original script text.

Happy path for this product: TTS word boundaries / alignment give the
timings, the **original script** gives the words (never the respelt
pronunciation text).  When the timed-token count matches the script-token
count, timed words are snapped to the script wording — the script is ground
truth (roadmap Part 6.7, "Subtitle hygiene").

Caption hygiene enforced here (roadmap constants):

* max 2 lines per cue, <= 42 characters per line;
* max 7 s per cue, phrase-boundary splits preferred;
* minimum cue duration (extended into the following gap, never overlapping
  the next cue); cues are monotonic and non-overlapping by construction;
* <= 20 chars/sec is a chunking goal — see :mod:`.validation` for the QC
  report when a dense cue cannot be extended.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from .models import Cue, WordTiming, coerce_word_timings

MAX_LINE_CHARS = 42
MAX_LINES_PER_CUE = 2
MAX_CUE_CHARS = MAX_LINE_CHARS * MAX_LINES_PER_CUE  # 84
MIN_CUE_DURATION_S = 0.8
MAX_CUE_DURATION_S = 7.0
MAX_CHARS_PER_SECOND = 20.0
MIN_CUE_GAP_S = 0.08

_SENTENCE_END_CHARS = ".!?…۔؟"
_PHRASE_END_CHARS = ",;:—–…"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…۔؟])\s+|\n+")


def split_script_sentences(script_text: str) -> list[str]:
    """Split a script into sentences (Latin + Urdu/Arabic terminators)."""
    if not script_text:
        return []
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(script_text.strip())]
    return [part for part in parts if part]


def format_timestamp(seconds: float) -> str:
    """Seconds -> SRT timestamp ``HH:MM:SS,mmm``."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000.0))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


_TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def parse_timestamp(value: str) -> float:
    match = _TIMESTAMP_RE.search(value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    hours, minutes, secs, ms = match.groups()
    millis = int(ms.ljust(3, "0"))
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + millis / 1000.0


def parse_srt(srt_text: str) -> list[Cue]:
    """Parse SRT text back into cues (round-trip / QC use)."""
    cues: list[Cue] = []
    blocks = re.split(r"\r?\n\s*\r?\n", srt_text.strip()) if srt_text.strip() else []
    for block in blocks:
        lines = [line.rstrip("\r") for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_idx = next(
            (i for i, line in enumerate(lines) if "-->" in line), None
        )
        if timing_idx is None:
            continue
        start_raw, end_raw = lines[timing_idx].split("-->", 1)
        text_lines = tuple(lines[timing_idx + 1 :])
        cues.append(
            Cue(
                index=len(cues) + 1,
                start=parse_timestamp(start_raw),
                end=parse_timestamp(end_raw.strip().split()[0]),
                lines=text_lines,
            )
        )
    return cues


def render_srt(cues: Sequence[Cue]) -> str:
    """Render cues as well-formed SRT text (1-based sequential indices)."""
    blocks: list[str] = []
    for position, cue in enumerate(cues, start=1):
        blocks.append(
            f"{position}\n"
            f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n"
            f"{cue.srt_text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


# ---------------------------------------------------------------------------
# Line wrapping
# ---------------------------------------------------------------------------


def _joined_len(words: Sequence[str]) -> int:
    return sum(len(w) for w in words) + max(0, len(words) - 1)


def wrap_words(
    words: Sequence[str],
    max_line_chars: int = MAX_LINE_CHARS,
    max_lines: int = MAX_LINES_PER_CUE,
) -> Optional[tuple[str, ...]]:
    """Wrap words into <= ``max_lines`` lines of <= ``max_line_chars``.

    Returns ``None`` when the words cannot fit (caller must split further).
    Lines are balanced: among feasible splits, the one minimising the
    longest line (then the line-length difference) wins.
    """
    word_list = [str(w) for w in words]
    if not word_list:
        return ()
    if any(len(w) > max_line_chars for w in word_list):
        return None
    if _joined_len(word_list) <= max_line_chars:
        return (" ".join(word_list),)
    if max_lines < 2:
        return None
    best: Optional[tuple[int, int, tuple[str, ...]]] = None
    for split in range(1, len(word_list)):
        first, second = word_list[:split], word_list[split:]
        len_first, len_second = _joined_len(first), _joined_len(second)
        if len_first > max_line_chars or len_second > max_line_chars:
            continue
        candidate = (
            max(len_first, len_second),
            abs(len_first - len_second),
            (" ".join(first), " ".join(second)),
        )
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best[2] if best else None


def wrap_text(
    text: str,
    max_line_chars: int = MAX_LINE_CHARS,
    max_lines: int = MAX_LINES_PER_CUE,
) -> Optional[tuple[str, ...]]:
    return wrap_words(text.split(), max_line_chars, max_lines)


# ---------------------------------------------------------------------------
# Cue construction
# ---------------------------------------------------------------------------


def _sentence_groups(
    timed: list[WordTiming], sentences: Sequence[str]
) -> list[list[WordTiming]]:
    """Group timed words by script sentence when token counts line up.

    Falls back to grouping on sentence-ending punctuation in the timed
    stream itself (e.g. whisper tokens that don't match the script 1:1).
    """
    sentence_tokens = [sentence.split() for sentence in sentences if sentence.split()]
    if sentence_tokens and sum(len(t) for t in sentence_tokens) == len(timed):
        groups: list[list[WordTiming]] = []
        cursor = 0
        for tokens in sentence_tokens:
            group: list[WordTiming] = []
            for token in tokens:
                source_word = timed[cursor]
                cursor += 1
                group.append(
                    WordTiming(
                        word=token,  # script wording is ground truth
                        start=source_word.start,
                        end=source_word.end,
                        source=source_word.source,
                        probability=source_word.probability,
                    )
                )
            if group:
                groups.append(group)
        return groups
    groups = []
    current: list[WordTiming] = []
    for word in timed:
        current.append(word)
        if word.word.rstrip().endswith(tuple(_SENTENCE_END_CHARS)):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _chunk_group(words: Sequence[WordTiming]) -> list[list[WordTiming]]:
    """Split one sentence's words into cue-sized chunks.

    A chunk must fit two 42-char lines and (for multi-word chunks) stay
    within the 7 s cap.  Splits prefer phrase boundaries (``,`` ``;`` ``:``
    dashes) over arbitrary word breaks.
    """
    chunks: list[list[WordTiming]] = []
    i = 0
    n = len(words)
    while i < n:
        # Largest prefix starting at i that still fits the cue constraints.
        best_end: Optional[int] = None
        j = i + 1
        while j <= n:
            candidate = words[i:j]
            texts = [w.word for w in candidate]
            if _joined_len(texts) > MAX_CUE_CHARS:
                break
            duration = candidate[-1].end - candidate[0].start
            if len(candidate) > 1 and duration > MAX_CUE_DURATION_S:
                break
            if wrap_words(texts) is not None:
                best_end = j
            j += 1
        if best_end is None:
            # Single word that cannot fit a line / exceeds caps on its own:
            # emit it alone rather than dropping text.
            best_end = i + 1
            chunks.append(list(words[i:best_end]))
            i = best_end
            continue
        if best_end >= n:
            chunks.append(list(words[i:]))
            break
        # Prefer the latest phrase boundary within the feasible prefix, as
        # long as it keeps a reasonable share of the chunk (>= 40%).
        split = best_end
        min_split = i + max(1, int((best_end - i) * 0.4))
        for k in range(best_end - 1, min_split - 1, -1):
            if words[k - 1].word.rstrip().endswith(tuple(_PHRASE_END_CHARS)):
                split = k
                break
        chunks.append(list(words[i:split]))
        i = split
    return chunks


def build_cues(
    word_timings: Iterable[object],
    script_sentences: Optional[Sequence[str] | str] = None,
) -> list[Cue]:
    """Build non-overlapping, hygiene-checked cues from word timings.

    ``script_sentences`` may be a list of sentences or a full script string.
    When its token count matches the timed words, script wording replaces
    timed wording (timings are kept).
    """
    timed = coerce_word_timings(word_timings)
    if not timed:
        return []
    if script_sentences is None:
        sentences: list[str] = []
    elif isinstance(script_sentences, str):
        sentences = split_script_sentences(script_sentences)
    else:
        sentences = [str(s) for s in script_sentences]

    raw_chunks: list[list[WordTiming]] = []
    for group in _sentence_groups(timed, sentences):
        raw_chunks.extend(_chunk_group(group))

    cues: list[Cue] = []
    prev_end: Optional[float] = None
    for position, chunk in enumerate(raw_chunks):
        lines = wrap_words([w.word for w in chunk]) or (" ".join(w.word for w in chunk),)
        start = chunk[0].start
        end = chunk[-1].end
        if prev_end is not None and start < prev_end + MIN_CUE_GAP_S:
            start = prev_end + MIN_CUE_GAP_S
        if end < start + MIN_CUE_DURATION_S:
            end = start + MIN_CUE_DURATION_S
        if len(chunk) > 1 and end - start > MAX_CUE_DURATION_S:
            end = start + MAX_CUE_DURATION_S
        next_start = (
            raw_chunks[position + 1][0].start
            if position + 1 < len(raw_chunks)
            else None
        )
        if next_start is not None and end > next_start - MIN_CUE_GAP_S:
            end = max(start + 0.04, next_start - MIN_CUE_GAP_S)
        if end <= start:
            end = start + 0.04
        cues.append(
            Cue(index=len(cues) + 1, start=start, end=end, lines=tuple(lines))
        )
        prev_end = end
    return cues


def build_srt(
    word_timings: Iterable[object],
    script_sentences: Optional[Sequence[str] | str] = None,
) -> str:
    """Convenience: word timings (+ script sentences) -> SRT text."""
    return render_srt(build_cues(word_timings, script_sentences))
