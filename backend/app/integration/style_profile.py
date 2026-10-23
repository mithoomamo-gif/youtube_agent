"""Style Lab: competitor style profiles + the synthesis engine (W1).

Style Lab lets a user point the app at 1..N competitor videos; an
analysis team (separate workers) measures each video and emits one
:class:`CompetitorVideoAnalysis` per video.  THIS module owns the two
shared data contracts and everything derived from them:

* :class:`CompetitorVideoAnalysis` — schema ``competitor_analysis.v1``,
  the analysis team's per-video output.  It is the ONLY place raw
  competitor SEO text (``seo.title_text`` / ``seo.description_text``)
  may live.
* :class:`StyleProfile` — schema ``style_profile.v1``, the aggregated,
  channel-scoped result every production stage consumes through
  :func:`style_directives`.
* :func:`synthesize_style_profile` — aggregates 1..N analyses into one
  profile: medians / percentile bands / rates, never examples.
* :func:`assert_no_copy` — the HARD no-copy gate (see below).
* :func:`resolve_style_profile`, :func:`save_profile`,
  :func:`load_profile` — how stages and the UI find/persist profiles.

THE NO-COPY RULE (hard boundary, enforced in code, not convention)
------------------------------------------------------------------
A :class:`StyleProfile` contains PATTERNS ONLY:

* never competitor footage, frames or thumbnail pixels;
* never competitor script text;
* never exact titles or descriptions — not even as "examples";
* title keywords are stored ONLY as a position enum
  (``start`` / ``middle`` / ``end`` / ``none``), never the keyword text.

The one place competitor identifiers appear is the provenance /
no-copy manifest (asset ids + sha256 hashes) — hashes and ids are not
content, and listing them there is what lets later stages REFUSE to
reuse those exact assets.  :func:`synthesize_style_profile` runs
:func:`assert_no_copy` before returning: if any source title /
description string, or any >= 4-word shingle of one, appears anywhere
in the serialised profile, synthesis raises instead of returning a
poisoned profile.

Resolution order for :func:`resolve_style_profile` (shared contract):

1. ``spec.style_profile_path`` (explicit per-project override);
2. ``<base_dir>/style_profiles/<channel_id>.json`` (channel default);
3. env ``YTA_STYLE_PROFILE_PATH`` (deployment default).

An explicitly configured path (1 or 3) that does not exist FAILS LOUD
(:class:`FileNotFoundError`) — per the project's R4-003 rule there is
no silent fallback away from an explicit choice.  When nothing is
configured and no channel profile exists, ``None`` is returned and the
pipeline runs exactly as it did before Style Lab.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids any cycle
    from app.integration.models import ProjectSpec

__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "STYLE_PROFILE_SCHEMA_VERSION",
    "STYLE_PROFILE_ENV_VAR",
    "CaptionsAnalysis",
    "ChapterSpan",
    "CompetitorVideoAnalysis",
    "DescriptionStructure",
    "DurationStats",
    "HookAnalysis",
    "HookStats",
    "MusicAnalysis",
    "NoCopyManifest",
    "NoCopyViolationError",
    "ProfileProvenance",
    "SeoTexts",
    "ShotSpan",
    "SourceRef",
    "SpeechAnalysis",
    "SpeechStats",
    "StyleProfile",
    "ThumbnailAnalysis",
    "ThumbnailStyle",
    "TitleFormula",
    "VisualMix",
    "assert_no_copy",
    "load_profile",
    "resolve_style_profile",
    "save_profile",
    "style_directives",
    "synthesize_style_profile",
]

#: Schema id carried by every :class:`CompetitorVideoAnalysis`.
ANALYSIS_SCHEMA_VERSION = "competitor_analysis.v1"
#: Schema id carried by every :class:`StyleProfile`.
STYLE_PROFILE_SCHEMA_VERSION = "style_profile.v1"
#: Env var consulted last by :func:`resolve_style_profile`.
STYLE_PROFILE_ENV_VAR = "YTA_STYLE_PROFILE_PATH"

#: Visual classes a shot may be labelled with by the analysis team.
VisualClass = Literal["stock", "ai", "plate", "unknown"]
#: Where the topic keyword sits in a title — a position, never the word.
KeywordPosition = Literal["start", "middle", "end", "none"]


class NoCopyViolationError(ValueError):
    """A profile (would) contain competitor title/description text."""


# ---------------------------------------------------------------------------
# CompetitorVideoAnalysis — schema "competitor_analysis.v1"
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceRef(_Strict):
    """Which competitor asset an analysis measured."""

    #: How the asset was obtained, e.g. "attached_file" / "youtube_url".
    kind: str = Field(..., min_length=1)
    #: Stable id of the source asset (never the content itself).
    asset_id: str = Field(..., min_length=1)
    #: Hex sha256 of the source asset bytes.
    sha256: str = Field(..., min_length=1)


class SpeechAnalysis(_Strict):
    """Measured narration of one competitor video."""

    wpm: float | None = Field(default=None, gt=0)
    language: str | None = None
    word_count: int | None = Field(default=None, ge=0)


class HookAnalysis(_Strict):
    """The opening hook of one competitor video."""

    #: Seconds from 0 until the hook lands / the cold open resolves.
    window_s: float | None = Field(default=None, ge=0)
    #: Hook pattern label, e.g. "question", "bold_claim", "cold_open".
    type: str | None = None


class ChapterSpan(_Strict):
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., gt=0)


class ShotSpan(_Strict):
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., gt=0)
    visual_class: VisualClass = "unknown"
    #: Classifier confidence in ``visual_class``, 0..1 (probabilistic —
    #: AI-vs-stock detection is a likelihood, not a fact).
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class CaptionsAnalysis(_Strict):
    present: bool = False
    burned_in: bool = False


class MusicAnalysis(_Strict):
    present: bool = False


class ThumbnailAnalysis(_Strict):
    """Measured thumbnail PATTERN of one video (never its pixels)."""

    layout_class: str | None = None
    text_word_count: int | None = Field(default=None, ge=0)
    face_present: bool | None = None
    palette_class: str | None = None


class SeoTexts(_Strict):
    """Raw public title/description — ANALYSIS SIDE ONLY.

    These strings are inputs to pattern extraction.  They must never
    survive into a :class:`StyleProfile`; :func:`assert_no_copy`
    enforces that.  Both default to ``None`` so an analysis without
    SEO text is valid and simply yields null patterns.
    """

    title_text: str | None = None
    description_text: str | None = None


class CompetitorVideoAnalysis(_Strict):
    """One competitor video, measured. The analysis team's contract."""

    schema_version: Literal["competitor_analysis.v1"] = ANALYSIS_SCHEMA_VERSION
    source: SourceRef
    duration_s: float = Field(..., gt=0)
    speech: SpeechAnalysis = Field(default_factory=SpeechAnalysis)
    hook: HookAnalysis = Field(default_factory=HookAnalysis)
    chapters: list[ChapterSpan] = Field(default_factory=list)
    shots: list[ShotSpan] = Field(default_factory=list)
    captions: CaptionsAnalysis = Field(default_factory=CaptionsAnalysis)
    music: MusicAnalysis = Field(default_factory=MusicAnalysis)
    thumbnail: ThumbnailAnalysis = Field(default_factory=ThumbnailAnalysis)
    seo: SeoTexts = Field(default_factory=SeoTexts)


# ---------------------------------------------------------------------------
# StyleProfile — schema "style_profile.v1"
# ---------------------------------------------------------------------------


class DurationStats(_Strict):
    """Runtime distribution across the analysed videos (seconds)."""

    median_s: float | None = None
    p25_s: float | None = None
    p75_s: float | None = None
    #: The duration new videos should target (the median).
    target_s: float | None = None


class SpeechStats(_Strict):
    wpm_median: float | None = None
    #: Dominant narration language across the analysed videos.
    language: str | None = None


class HookStats(_Strict):
    window_s: float | None = None
    #: Distinct hook pattern labels seen, most frequent first.
    types: list[str] = Field(default_factory=list)


class ChapterStats(_Strict):
    #: Chapter count new videos should aim for (median, rounded).
    target_count: int | None = None
    #: Typical chapter length in seconds.
    avg_chapter_s: float | None = None


class VisualMix(_Strict):
    """Visual-source mix, as fractions of total analysed shot time.

    The denominator is ALL shot time (including ``plate`` and
    ``unknown``), so ``stock_ratio + ai_ratio`` may be < 1 — that gap
    is honest uncertainty, not a rounding error.
    """

    stock_ratio: float | None = None
    ai_ratio: float | None = None
    avg_shot_len_s: float | None = None
    cuts_per_min: float | None = None


class CaptionStats(_Strict):
    present_rate: float | None = None
    burned_in_rate: float | None = None
    #: Majority verdict (>= half of analysed videos burn captions in).
    burned_in: bool = False


class MusicStats(_Strict):
    present_rate: float | None = None
    #: Majority verdict (>= half of analysed videos carry music).
    expected: bool = False


class ThumbnailStyle(_Strict):
    """Thumbnail PATTERN (layout/palette classes — never pixels)."""

    #: Most common layout class, e.g. "face_right_text_left".
    layout_class: str | None = None
    text_words_min: int | None = None
    text_words_max: int | None = None
    face_rate: float | None = None
    #: Majority verdict (>= half of known thumbnails show a face).
    face_expected: bool = False
    #: Most common palette class, e.g. "high_contrast_warm".
    palette_class: str | None = None


class TitleFormula(_Strict):
    """Title PATTERNS extracted from features only.

    No title text, and no keyword text, is ever stored here — the
    keyword survives only as ``keyword_position``.
    """

    #: Character-length band across analysed titles.
    len_min: int | None = None
    len_max: int | None = None
    #: Fraction of titles containing a digit ("Top 10 ...").
    number_rate: float | None = None
    #: Fraction of titles containing a bracket or pipe: [](){}|
    bracket_rate: float | None = None
    #: Mean fraction of title words that are ALL-CAPS (len >= 2).
    caps_rate: float | None = None
    #: Where the topic keyword sits: start / middle / end / none.
    keyword_position: KeywordPosition | None = None


class DescriptionStructure(_Strict):
    """Description PATTERN: ordered section labels + counts only."""

    #: Ordered section labels, e.g. ["intro", "chapters", "links",
    #: "cta", "hashtags"]. Labels only — never the section text.
    sections: list[str] = Field(default_factory=list)
    hashtag_min: int | None = None
    hashtag_max: int | None = None
    #: Majority verdict (>= half of descriptions contain a link).
    link_expected: bool = False


class ProfileProvenance(_Strict):
    """Which competitor assets this profile was derived from."""

    source_asset_ids: list[str] = Field(default_factory=list)
    source_sha256: list[str] = Field(default_factory=list)


class NoCopyManifest(_Strict):
    """The profile's own no-copy guarantee, carried with the data."""

    never_reuse_asset_ids: list[str] = Field(default_factory=list)
    never_reuse_sha256: list[str] = Field(default_factory=list)
    #: A StyleProfile NEVER contains competitor assets; the literal
    #: ``False`` makes any hand-crafted profile claiming otherwise
    #: fail validation.
    contains_competitor_assets: Literal[False] = False


class StyleProfile(_Strict):
    """Aggregated competitor STYLE for one of the user's channels.

    Patterns only — see the module docstring's no-copy rule.  Every
    numeric field is ``None`` when the underlying analyses carried no
    measurement for it (e.g. no SEO texts supplied), so consumers must
    treat ``None`` as "no directive", never as zero.
    """

    schema_version: Literal["style_profile.v1"] = STYLE_PROFILE_SCHEMA_VERSION
    #: The USER's channel this profile is scoped to.
    channel_id: str = Field(..., min_length=1)
    #: How many competitor videos were aggregated.
    video_count: int = Field(default=0, ge=0)
    duration: DurationStats = Field(default_factory=DurationStats)
    speech: SpeechStats = Field(default_factory=SpeechStats)
    hook: HookStats = Field(default_factory=HookStats)
    chapters: ChapterStats = Field(default_factory=ChapterStats)
    visuals: VisualMix = Field(default_factory=VisualMix)
    captions: CaptionStats = Field(default_factory=CaptionStats)
    music: MusicStats = Field(default_factory=MusicStats)
    thumbnail: ThumbnailStyle = Field(default_factory=ThumbnailStyle)
    title_formula: TitleFormula = Field(default_factory=TitleFormula)
    description_structure: DescriptionStructure = Field(
        default_factory=DescriptionStructure)
    provenance: ProfileProvenance = Field(default_factory=ProfileProvenance)
    no_copy_manifest: NoCopyManifest = Field(default_factory=NoCopyManifest)

    @field_validator("channel_id")
    @classmethod
    def _channel_id_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("channel_id must not be blank")
        return cleaned


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _median(values: list[float]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return float(statistics.median(clean))


def _percentile(values: list[float], fraction: float) -> float | None:
    """Inclusive percentile; exact for the small N Style Lab sees."""
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    rank = fraction * (len(clean) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return clean[int(rank)]
    weight = rank - low
    return clean[low] * (1.0 - weight) + clean[high] * weight


def _rate(flags: list[bool]) -> float | None:
    if not flags:
        return None
    return sum(1 for f in flags if f) / len(flags)


def _majority(rate: float | None) -> bool:
    return bool(rate is not None and rate >= 0.5)


def _mode(values: list[str | None]) -> str | None:
    """Most frequent non-empty value; ties keep first-seen order."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for value in values:
        if not value:
            continue
        if value not in counts:
            order.append(value)
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    return max(order, key=lambda v: counts[v])


def _ranked(values: list[str | None]) -> list[str]:
    """Distinct non-empty values, most frequent first."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for value in values:
        if not value:
            continue
        if value not in counts:
            order.append(value)
        counts[value] = counts.get(value, 0) + 1
    return sorted(order, key=lambda v: -counts[v])


# ---------------------------------------------------------------------------
# Title formula — FEATURES ONLY (the keyword itself is never stored)
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "into", "is", "it", "its", "of", "on", "or", "that",
    "the", "this", "to", "was", "what", "when", "why", "will", "with",
    "you", "your",
})

_BRACKET_CHARS = frozenset("[](){}|")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _title_words(title: str) -> list[str]:
    return _WORD_RE.findall(title)


def _is_caps_word(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def _keyword_position_for_titles(titles: list[str]) -> KeywordPosition | None:
    """Approximate where the TOPIC keyword sits, per title, then vote.

    The topic keyword is approximated WITHOUT storing it: with 2+
    titles it is the most frequent content (non-stopword) word shared
    across titles; with a single title it is that title's longest
    content word.  Only each title's position bucket survives — the
    word itself is a local variable and is never returned or stored.
    """
    per_title_words: list[list[str]] = []
    for title in titles:
        words = [w.lower() for w in _title_words(title)]
        per_title_words.append(words)
    content: list[list[str]] = [
        [w for w in words if w not in _STOPWORDS and len(w) >= 3]
        for words in per_title_words
    ]
    if not any(content):
        return "none" if titles else None

    keyword: str | None = None
    if len(titles) >= 2:
        shared: dict[str, int] = {}
        for words in content:
            for word in set(words):
                shared[word] = shared.get(word, 0) + 1
        repeated = {w: c for w, c in shared.items() if c >= 2}
        if repeated:
            keyword = max(repeated, key=lambda w: repeated[w])
    if keyword is None:
        first = next((ws for ws in content if ws), [])
        if not first:
            return "none"
        keyword = max(first, key=len)

    buckets: list[str] = []
    for words in per_title_words:
        if keyword not in words or not words:
            continue
        fraction = words.index(keyword) / max(len(words) - 1, 1)
        if len(words) == 1 or fraction < 1 / 3:
            buckets.append("start")
        elif fraction < 2 / 3:
            buckets.append("middle")
        else:
            buckets.append("end")
    if not buckets:
        return "none"
    return _mode(buckets)  # type: ignore[return-value]


def _title_formula(titles: list[str]) -> TitleFormula:
    if not titles:
        return TitleFormula()
    lengths = [len(t) for t in titles]
    caps_rates: list[float] = []
    for title in titles:
        words = _title_words(title)
        if words:
            caps_rates.append(
                sum(1 for w in words if _is_caps_word(w)) / len(words))
    return TitleFormula(
        len_min=min(lengths),
        len_max=max(lengths),
        number_rate=_round(_rate([any(c.isdigit() for c in t)
                                  for t in titles])),
        bracket_rate=_round(_rate([any(c in _BRACKET_CHARS for c in t)
                                   for t in titles])),
        caps_rate=_round(
            sum(caps_rates) / len(caps_rates) if caps_rates else None),
        keyword_position=_keyword_position_for_titles(titles),
    )


# ---------------------------------------------------------------------------
# Description structure — ordered section LABELS only
# ---------------------------------------------------------------------------

_TIMESTAMP_LINE_RE = re.compile(r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\b")
_LINK_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)
_CTA_RE = re.compile(
    r"\b(subscribe|follow|join|download|check out|watch next|sign up|"
    r"link in|comment below|like and|share this)\b",
    re.IGNORECASE,
)


def _description_sections(text: str) -> tuple[list[str], int, bool]:
    """Detect section labels in order of first appearance in ONE text.

    Returns ``(ordered_labels, hashtag_count, has_link)``.  Only the
    labels/counts leave this function — never the text itself.
    """
    lines = [line.strip() for line in text.splitlines()]
    found: dict[str, int] = {}  # label -> first line index

    def mark(label: str, index: int) -> None:
        if label not in found:
            found[label] = index

    timestamp_lines = 0
    hashtag_count = 0
    has_link = False
    first_text_index: int | None = None
    for index, line in enumerate(lines):
        if not line:
            continue
        if first_text_index is None:
            first_text_index = index
        if _TIMESTAMP_LINE_RE.match(line):
            timestamp_lines += 1
            if timestamp_lines >= 2:
                mark("chapters", index)
            continue
        tags = _HASHTAG_RE.findall(line)
        if tags:
            hashtag_count += len(tags)
            mark("hashtags", index)
            continue
        if _LINK_RE.search(line):
            has_link = True
            mark("links", index)
            continue
        if _CTA_RE.search(line):
            mark("cta", index)
    # Any prose before the first detected special section is the intro.
    if first_text_index is not None:
        special = [i for label, i in found.items() if label != "hashtags"]
        if not special or first_text_index < min(special):
            mark("intro", first_text_index)
    ordered = [label for label, _ in sorted(found.items(),
                                            key=lambda kv: kv[1])]
    return ordered, hashtag_count, has_link


def _description_structure(texts: list[str]) -> DescriptionStructure:
    if not texts:
        return DescriptionStructure()
    per_text: list[tuple[list[str], int, bool]] = [
        _description_sections(t) for t in texts]
    # Union of labels, ordered by their average first-appearance rank.
    ranks: dict[str, list[float]] = {}
    for labels, _, _ in per_text:
        for rank, label in enumerate(labels):
            ranks.setdefault(label, []).append(float(rank))
    sections = sorted(
        ranks, key=lambda label: sum(ranks[label]) / len(ranks[label]))
    hashtag_counts = [count for _, count, _ in per_text]
    return DescriptionStructure(
        sections=sections,
        hashtag_min=min(hashtag_counts) if hashtag_counts else None,
        hashtag_max=max(hashtag_counts) if hashtag_counts else None,
        link_expected=_majority(_rate([link for _, _, link in per_text])),
    )


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def synthesize_style_profile(
    analyses: list[CompetitorVideoAnalysis],
    channel_id: str,
) -> StyleProfile:
    """Aggregate 1..N competitor analyses into one :class:`StyleProfile`.

    Statistics are medians / percentile bands / rates over whatever
    measurements the analyses actually carry; missing measurements
    yield ``None`` pattern fields, never a crash and never a fabricated
    zero.  The result is passed through :func:`assert_no_copy` before
    it is returned.
    """
    if not analyses:
        raise ValueError(
            "synthesize_style_profile needs at least one analysis")
    if not str(channel_id or "").strip():
        raise ValueError("channel_id must not be blank")

    durations = [a.duration_s for a in analyses]

    # -- chapters ------------------------------------------------------
    chapter_counts = [len(a.chapters) for a in analyses if a.chapters]
    chapter_lengths: list[float] = []
    for a in analyses:
        if a.chapters:
            chapter_lengths.extend(
                max(c.end_s - c.start_s, 0.0) for c in a.chapters)
    chapters_target = (
        int(round(float(statistics.median(chapter_counts))))
        if chapter_counts else None)
    avg_chapter_s = _round(_median(chapter_lengths), 2)
    if avg_chapter_s is None and chapters_target:
        median_duration = _median(durations)
        if median_duration:
            avg_chapter_s = _round(median_duration / chapters_target, 2)

    # -- visuals ---------------------------------------------------------
    stock_s = ai_s = total_s = 0.0
    shot_lengths: list[float] = []
    total_shots = 0
    for a in analyses:
        for shot in a.shots:
            length = max(shot.end_s - shot.start_s, 0.0)
            shot_lengths.append(length)
            total_s += length
            if shot.visual_class == "stock":
                stock_s += length
            elif shot.visual_class == "ai":
                ai_s += length
        total_shots += len(a.shots)
    total_minutes = sum(durations) / 60.0
    visuals = VisualMix(
        stock_ratio=_round(stock_s / total_s) if total_s else None,
        ai_ratio=_round(ai_s / total_s) if total_s else None,
        avg_shot_len_s=_round(_median(shot_lengths), 2),
        cuts_per_min=_round(total_shots / total_minutes, 2)
        if total_shots and total_minutes else None,
    )

    # -- thumbnails --------------------------------------------------------
    face_flags = [a.thumbnail.face_present for a in analyses
                  if a.thumbnail.face_present is not None]
    face_rate = _rate(face_flags)
    word_counts = [a.thumbnail.text_word_count for a in analyses
                   if a.thumbnail.text_word_count is not None]
    thumbnail = ThumbnailStyle(
        layout_class=_mode([a.thumbnail.layout_class for a in analyses]),
        text_words_min=min(word_counts) if word_counts else None,
        text_words_max=max(word_counts) if word_counts else None,
        face_rate=_round(face_rate),
        face_expected=_majority(face_rate),
        palette_class=_mode([a.thumbnail.palette_class for a in analyses]),
    )

    # -- SEO patterns (raw texts are consumed here and NOWHERE else) -----
    titles = [a.seo.title_text.strip() for a in analyses
              if a.seo.title_text and a.seo.title_text.strip()]
    descriptions = [a.seo.description_text.strip() for a in analyses
                    if a.seo.description_text
                    and a.seo.description_text.strip()]

    caption_rate = _rate([a.captions.burned_in for a in analyses])
    profile = StyleProfile(
        channel_id=str(channel_id).strip(),
        video_count=len(analyses),
        duration=DurationStats(
            median_s=_round(_median(durations), 2),
            p25_s=_round(_percentile(durations, 0.25), 2),
            p75_s=_round(_percentile(durations, 0.75), 2),
            target_s=_round(_median(durations), 2),
        ),
        speech=SpeechStats(
            wpm_median=_round(_median(
                [a.speech.wpm for a in analyses
                 if a.speech.wpm is not None]), 2),
            language=_mode([a.speech.language for a in analyses]),
        ),
        hook=HookStats(
            window_s=_round(_median(
                [a.hook.window_s for a in analyses
                 if a.hook.window_s is not None]), 2),
            types=_ranked([a.hook.type for a in analyses]),
        ),
        chapters=ChapterStats(
            target_count=chapters_target, avg_chapter_s=avg_chapter_s),
        visuals=visuals,
        captions=CaptionStats(
            present_rate=_round(_rate([a.captions.present
                                       for a in analyses])),
            burned_in_rate=_round(caption_rate),
            burned_in=_majority(caption_rate),
        ),
        music=MusicStats(
            present_rate=_round(_rate([a.music.present for a in analyses])),
            expected=_majority(_rate([a.music.present for a in analyses])),
        ),
        thumbnail=thumbnail,
        title_formula=_title_formula(titles),
        description_structure=_description_structure(descriptions),
        provenance=ProfileProvenance(
            source_asset_ids=[a.source.asset_id for a in analyses],
            source_sha256=[a.source.sha256 for a in analyses],
        ),
        no_copy_manifest=NoCopyManifest(
            never_reuse_asset_ids=[a.source.asset_id for a in analyses],
            never_reuse_sha256=[a.source.sha256 for a in analyses],
            contains_competitor_assets=False,
        ),
    )
    assert_no_copy(profile, analyses)
    return profile


# ---------------------------------------------------------------------------
# No-copy enforcement
# ---------------------------------------------------------------------------

_SHINGLE_WORDS = 4


def _normalise_text(text: str) -> str:
    """Lowercase, punctuation-stripped, whitespace-collapsed text."""
    lowered = text.lower()
    scrubbed = "".join(c if c.isalnum() else " " for c in lowered)
    return " ".join(scrubbed.split())


def _shingles(text: str, size: int = _SHINGLE_WORDS) -> set[str]:
    words = _normalise_text(text).split()
    return {" ".join(words[i:i + size])
            for i in range(len(words) - size + 1)}


def assert_no_copy(
    profile: StyleProfile,
    analyses: list[CompetitorVideoAnalysis],
) -> None:
    """Fail if any competitor title/description text leaked into a profile.

    Checks, against the profile's full serialised JSON (normalised):
    the complete title and description strings, and every
    >= 4-word shingle of them.  Raises :class:`NoCopyViolationError`
    on the first leak found; returns ``None`` when the profile is
    clean.  Asset ids / sha256 hashes in the provenance and no-copy
    manifest are identifiers, not content, and are not checked.
    """
    blob = _normalise_text(profile.model_dump_json())
    for analysis in analyses:
        for label, raw in (("title", analysis.seo.title_text),
                           ("description", analysis.seo.description_text)):
            if not raw or not raw.strip():
                continue
            whole = _normalise_text(raw)
            if whole and whole in blob:
                raise NoCopyViolationError(
                    f"no-copy violation: competitor {label} text appears "
                    f"verbatim in the style profile "
                    f"(asset {analysis.source.asset_id!r})")
            for shingle in _shingles(raw):
                if shingle in blob:
                    raise NoCopyViolationError(
                        f"no-copy violation: a {_SHINGLE_WORDS}-word "
                        f"shingle of the competitor {label} appears in "
                        f"the style profile "
                        f"(asset {analysis.source.asset_id!r})")


# ---------------------------------------------------------------------------
# Directives — the flat dict every stage consumes
# ---------------------------------------------------------------------------


def style_directives(profile: StyleProfile | None) -> dict[str, Any]:
    """Flatten a profile into the shared ``style_directives`` dict.

    Keys (shared contract): ``target_duration_s``, ``speech_wpm``,
    ``hook_window_s``, ``hook_types``, ``chapters_target``,
    ``avg_chapter_s``, ``stock_ratio``, ``ai_ratio``,
    ``avg_shot_len_s``, ``cuts_per_min``, ``captions_burned_in``,
    ``music_expected``, ``thumbnail`` {layout_class, text_words_min,
    text_words_max, face_expected, palette_class}, ``title_formula``
    {len_min, len_max, number_rate, bracket_rate, caps_rate,
    keyword_position}, ``description_structure`` {sections,
    hashtag_min, hashtag_max, link_expected}.

    ``None`` profile -> ``{}`` (no directives; stages run unstyled).
    Individual values stay ``None`` when the profile has no
    measurement for them — stages must treat ``None`` as "no opinion".
    """
    if profile is None:
        return {}
    return {
        "target_duration_s": profile.duration.target_s,
        "speech_wpm": profile.speech.wpm_median,
        "hook_window_s": profile.hook.window_s,
        "hook_types": list(profile.hook.types),
        "chapters_target": profile.chapters.target_count,
        "avg_chapter_s": profile.chapters.avg_chapter_s,
        "stock_ratio": profile.visuals.stock_ratio,
        "ai_ratio": profile.visuals.ai_ratio,
        "avg_shot_len_s": profile.visuals.avg_shot_len_s,
        "cuts_per_min": profile.visuals.cuts_per_min,
        "captions_burned_in": profile.captions.burned_in,
        "music_expected": profile.music.expected,
        "thumbnail": {
            "layout_class": profile.thumbnail.layout_class,
            "text_words_min": profile.thumbnail.text_words_min,
            "text_words_max": profile.thumbnail.text_words_max,
            "face_expected": profile.thumbnail.face_expected,
            "palette_class": profile.thumbnail.palette_class,
        },
        "title_formula": {
            "len_min": profile.title_formula.len_min,
            "len_max": profile.title_formula.len_max,
            "number_rate": profile.title_formula.number_rate,
            "bracket_rate": profile.title_formula.bracket_rate,
            "caps_rate": profile.title_formula.caps_rate,
            "keyword_position": profile.title_formula.keyword_position,
        },
        "description_structure": {
            "sections": list(profile.description_structure.sections),
            "hashtag_min": profile.description_structure.hashtag_min,
            "hashtag_max": profile.description_structure.hashtag_max,
            "link_expected": profile.description_structure.link_expected,
        },
    }


# ---------------------------------------------------------------------------
# Persistence + resolution
# ---------------------------------------------------------------------------


def save_profile(profile: StyleProfile, path: str | Path) -> Path:
    """Write a profile as indented JSON; returns the path written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(profile.model_dump(mode="json"), indent=2,
                   ensure_ascii=False) + "\n",
        encoding="utf-8")
    return target


def load_profile(path: str | Path) -> StyleProfile:
    """Load + validate a profile previously written by save_profile."""
    return StyleProfile.model_validate_json(
        Path(path).read_text(encoding="utf-8"))


def _spec_value(spec: Any, name: str) -> Any:
    """Read a field from a ProjectSpec, a dict, or any lookalike."""
    if spec is None:
        return None
    if isinstance(spec, dict):
        return spec.get(name)
    return getattr(spec, name, None)


def _load_explicit(path_value: Any, origin: str) -> StyleProfile:
    path = Path(str(path_value)).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"style profile from {origin} does not exist: {path}")
    return load_profile(path)


def resolve_style_profile(
    spec: "ProjectSpec | Any",
    base_dir: str | Path,
) -> StyleProfile | None:
    """Resolve the style profile for one project, or ``None``.

    Order (shared contract): (1) ``spec.style_profile_path``;
    (2) ``<base_dir>/style_profiles/<channel_id>.json``;
    (3) env ``YTA_STYLE_PROFILE_PATH``.  Explicit paths (1 and 3)
    fail loud when missing; the channel default (2) applies only
    when the file exists.
    """
    explicit = _spec_value(spec, "style_profile_path")
    if explicit:
        return _load_explicit(explicit, "spec.style_profile_path")

    channel_id = _spec_value(spec, "channel_id")
    if channel_id:
        candidate = (Path(base_dir) / "style_profiles"
                     / f"{channel_id}.json")
        if candidate.is_file():
            return load_profile(candidate)

    env_path = os.environ.get(STYLE_PROFILE_ENV_VAR)
    if env_path:
        return _load_explicit(env_path, f"env {STYLE_PROFILE_ENV_VAR}")
    return None
