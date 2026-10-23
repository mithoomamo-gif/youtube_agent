"""Chunking engine — the core deliverable of the TTS layer.

Pipeline: locked script -> normalize -> pronunciation dictionary ->
split CHAPTER -> paragraph -> sentence-cluster -> Chunk.

Rules (roadmap section 30):
* target 800-1,500 chars per chunk, HARD CAP 2,800 chars
* never split mid-sentence (a single over-cap sentence is split only at
  clause boundaries, then at word boundaries, as a documented last resort)
* one chapter = one continuity unit (provider/voice/preset pinned per chapter
  by the engine, not here)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CHUNK_TARGET_MIN = 800
CHUNK_TARGET_MAX = 1500
CHUNK_HARD_CAP = 2800

_SENTENCE_END = set(".!?।۔؟")
_ABBREV_PROTECT = [
    "Mr.", "Mrs.", "Ms.", "Dr.", "St.", "Prof.", "Sr.", "Jr.", "vs.",
    "e.g.", "i.e.", "U.S.", "U.K.", "a.m.", "p.m.",
]


@dataclass
class Chapter:
    chapter_no: int
    text: str
    title: str | None = None


@dataclass
class Chunk:
    video_id: str
    chapter_no: int
    chunk_no: int                      # global, restores order at stitch time
    chunk_no_in_chapter: int
    original_text: str                 # pre-dictionary text (subtitle source)
    text: str                          # synthesis text (dictionary-compiled
                                       # per provider at render time if needed)
    voice_id: str = ""
    preset: str | None = None
    dict_version: str = ""
    flagged_tokens: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)


def split_sentences(text: str) -> list[str]:
    """Split on sentence terminators across Latin / Urdu / Devanagari scripts
    without splitting decimals or common abbreviations."""
    protected = text
    placeholders: dict[str, str] = {}
    for i, abbr in enumerate(_ABBREV_PROTECT):
        if abbr in protected:
            ph = f"\x00{i}\x00"
            placeholders[ph] = abbr
            protected = protected.replace(abbr, ph)
    protected = re.sub(r"(\d)\.(\d)",
                       lambda m: f"{m.group(1)}\x01{m.group(2)}", protected)

    sentences: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(protected):
        ch = protected[i]
        buf.append(ch)
        if ch in _SENTENCE_END:
            # Consume closing quotes/brackets and trailing space.
            j = i + 1
            while j < len(protected) and protected[j] in "\"'”’)] ":
                buf.append(protected[j])
                j += 1
            piece = "".join(buf).strip()
            if piece:
                sentences.append(piece)
            buf = []
            i = j
            continue
        i += 1
    tail = "".join(buf).strip()
    if tail:
        sentences.append(tail)

    restored = []
    for s in sentences:
        for ph, abbr in placeholders.items():
            s = s.replace(ph, abbr)
        restored.append(s.replace("\x01", "."))
    return restored


def _split_long_sentence(sentence: str, cap: int) -> list[str]:
    """Last-resort split of a single sentence longer than the hard cap:
    clause boundaries first (comma/semicolon/colon), then word boundaries.
    Still never splits a word."""
    if len(sentence) <= cap:
        return [sentence]
    pieces: list[str] = []
    rest = sentence
    while len(rest) > cap:
        window = rest[:cap]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(": "),
                  window.rfind(" — "), window.rfind("، "))
        if cut < cap // 3:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = cap
        pieces.append(rest[:cut + 1].strip() if rest[cut] in ",;:" else rest[:cut].strip())
        rest = rest[cut + 1:].strip() if rest[cut] in ",;:" else rest[cut:].strip()
        if not rest:
            break
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p]


def _pack(sentences: list[str], target_max: int, cap: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        for piece in _split_long_sentence(sentence, cap):
            piece_len = len(piece) + (1 if current else 0)
            if current and current_len + piece_len > target_max:
                chunks.append(" ".join(current))
                current, current_len = [piece], len(piece)
            else:
                current.append(piece)
                current_len += piece_len
    if current:
        chunks.append(" ".join(current))
    # Merge a tiny trailing chunk into its predecessor when the cap allows —
    # keeps chunks inside the 800-1,500 target band where possible.
    if len(chunks) >= 2 and len(chunks[-1]) < CHUNK_TARGET_MIN // 2 \
            and len(chunks[-2]) + 1 + len(chunks[-1]) <= cap:
        chunks[-2] = chunks[-2] + " " + chunks[-1]
        chunks.pop()
    return chunks


def parse_script(script) -> list[Chapter]:
    """Accept a raw string (with ``#``/``##`` headings or ``Chapter N``
    markers), a list of Chapter/dict, or a single Chapter."""
    if isinstance(script, Chapter):
        return [script]
    if isinstance(script, (list, tuple)):
        chapters: list[Chapter] = []
        for i, item in enumerate(script, start=1):
            if isinstance(item, Chapter):
                chapters.append(Chapter(i, item.text, item.title))
            elif isinstance(item, dict):
                chapters.append(Chapter(
                    int(item.get("chapter_no", i)),
                    item.get("text", ""),
                    item.get("title")))
            else:
                chapters.append(Chapter(i, str(item)))
        return chapters
    text = str(script)
    parts = re.split(
        r"(?m)^(?:#{1,6}\s*(?:Chapter\s+\d+[:\-.]?\s*)?(.*)|"
        r"Chapter\s+\d+\s*[:\-]\s*(.*))\s*$", text)
    if len(parts) <= 1:
        return [Chapter(1, text.strip())]
    chapters = []
    # re.split with 2 groups -> [pre, g1, g2, body, g1, g2, body, ...]
    pre = parts[0].strip()
    if pre:
        chapters.append(Chapter(len(chapters) + 1, pre))
    idx = 1
    while idx < len(parts):
        title = parts[idx] or parts[idx + 1]
        body = parts[idx + 2].strip() if idx + 2 < len(parts) else ""
        if body:
            chapters.append(Chapter(len(chapters) + 1, body, title or None))
        idx += 3
    return chapters or [Chapter(1, text.strip())]


def chunk_chapter_text(text: str, *, target_max: int = CHUNK_TARGET_MAX,
                       cap: int = CHUNK_HARD_CAP) -> list[str]:
    """Split one chapter's text into chunk strings (paragraph-aware)."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()] if text.strip() else []
    chunks: list[str] = []
    pending: list[str] = []
    for para in paragraphs:
        sentences = split_sentences(para)
        packed = _pack(sentences, target_max, cap)
        # Paragraph boundaries are preferred chunk boundaries: flush a short
        # pending cluster before a paragraph that packs into big chunks.
        for piece in packed:
            if pending and len(" ".join(pending)) + 1 + len(piece) > target_max:
                chunks.append(" ".join(pending))
                pending = [piece]
            else:
                pending.append(piece)
    if pending:
        chunks.append(" ".join(pending))
    return [c for c in chunks if c.strip()]
