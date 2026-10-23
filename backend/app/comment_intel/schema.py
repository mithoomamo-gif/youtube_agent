"""Comment Intelligence (Agent 28, Round 5) — result schema.

A *rollup* is the unit this engine produces: one per video, and one
aggregated per channel.  Every rollup carries a :class:`Provenance`
block — the Round 4 R4-003 honesty law applies here in full: an
analysis produced by the deterministic fallback is LABELLED
``engine='deterministic'`` (with ``fallback_reason`` set when an LLM
was attempted and failed); it is never presented as an LLM result.

Sentiment bands are mutually exclusive and sum to 1.0 over the
*analysed* (non-spam) comments: a comment that asks a question lands
in the ``question`` band (viewers' questions are the highest-value
signal for the learning loop, so they are not diluted into
positive/neutral), otherwise positive / neutral / negative by the
documented lexicon method (see ``provenance.sentiment_method`` —
sentiment is lexicon-based in BOTH engine modes in this round).

Question text is viewers' public comment text, quoted verbatim (the
exact sentence from the comment) for the channel owner's own review.
It is never paraphrased.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CommentInput",
    "Provenance",
    "SentimentBands",
    "ThemeCluster",
    "TopQuestion",
    "TopicItem",
    "SpamFlag",
    "CommentRollup",
    "LLMTheme",
    "LLMQuestion",
    "LLMTopic",
    "LLMCommentAnalysis",
    "comment_from_row",
]


class CommentInput(BaseModel):
    """One stored comment, in the shape the comments store documents.

    Fields mirror the schema worker's contract: ``id``, ``video_id``,
    ``channel_id``, ``author_display_name``, ``text``, ``like_count``,
    ``published_at``, ``reply_count``.  Extra stored fields are ignored
    so rows from the real store drop in unchanged.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    video_id: str | None = None
    channel_id: str | None = None
    author_display_name: str | None = None
    text: str = ""
    like_count: int = 0
    published_at: Any = None
    reply_count: int = 0


def comment_from_row(row: Any) -> CommentInput:
    """Coerce a plain dict / sqlite3.Row / CommentInput into CommentInput."""
    if isinstance(row, CommentInput):
        return row
    if isinstance(row, dict):
        return CommentInput(**row)
    # sqlite3.Row and other mapping-likes
    try:
        keys = row.keys()  # type: ignore[attr-defined]
    except Exception:
        return CommentInput(id=str(getattr(row, "id")))
    return CommentInput(**{k: row[k] for k in keys})


class Provenance(BaseModel):
    """Which engine produced a rollup — always present, always honest.

    ``engine``: ``'llm'`` when the LLM router produced the theme /
    question / topic layer, ``'deterministic'`` when the built-in
    deterministic analyser did.  ``model`` is the physical model id
    reported by the router for LLM runs, else ``None``.
    ``fallback_reason`` is set ONLY on a deterministic run that
    followed a failed/unavailable LLM attempt (exception class +
    message, or ``'llm_not_configured'``); a deterministic run that
    was requested directly carries ``fallback_reason=None`` and
    ``engine='deterministic'`` — still fully labelled.
    ``sentiment_method`` names the sentiment method in both modes
    (``'lexicon_v1'`` this round).  ``spam_method`` likewise
    (``'heuristics_v1'`` in both modes).
    """

    engine: Literal["llm", "deterministic"]
    model: str | None = None
    generated_at: str  # ISO-8601 UTC
    sentiment_method: str = "lexicon_v1"
    spam_method: str = "heuristics_v1"
    fallback_reason: str | None = None


class SentimentBands(BaseModel):
    """Shares over analysed (non-spam) comments; sum to 1.0 (or all 0
    when there are no analysed comments)."""

    positive: float = 0.0
    neutral: float = 0.0
    negative: float = 0.0
    question: float = 0.0


class ThemeCluster(BaseModel):
    label: str
    comment_ids: list[str] = Field(default_factory=list)
    share: float = 0.0  # of analysed comments


class TopQuestion(BaseModel):
    """TOP-QUESTIONS SEAM item (see docs/COMMENTS.md).

    ``question_text`` is the verbatim question sentence from the
    viewer's comment — never paraphrased.
    """

    question_text: str
    video_id: str | None = None
    comment_id: str
    like_count: int = 0


class TopicItem(BaseModel):
    label: str
    comment_ids: list[str] = Field(default_factory=list)
    count: int = 0


class SpamFlag(BaseModel):
    """One spam/abuse flag — feeds the moderation queue owned by the
    engagement worker (persisted to ``comment_moderation_flags`` when
    that table exists; see app.comment_intel.storage)."""

    comment_id: str
    video_id: str | None = None
    channel_id: str | None = None
    reason: str


class CommentRollup(BaseModel):
    """Per-video or per-channel comment analysis rollup."""

    scope: Literal["video", "channel"]
    video_id: str | None = None
    channel_id: str | None = None
    comment_count: int = 0  # total comments handed to the engine
    analysed_count: int = 0  # non-spam comments the bands/themes cover
    spam_count: int = 0
    sentiment: SentimentBands = Field(default_factory=SentimentBands)
    themes: list[ThemeCluster] = Field(default_factory=list)
    top_questions: list[TopQuestion] = Field(default_factory=list)
    praise_topics: list[TopicItem] = Field(default_factory=list)
    complaint_topics: list[TopicItem] = Field(default_factory=list)
    spam_flags: list[SpamFlag] = Field(default_factory=list)
    provenance: Provenance
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM structured-output schema (the router validates against this).
# ---------------------------------------------------------------------------


class LLMTheme(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: str
    comment_ids: list[str] = Field(default_factory=list)


class LLMQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")
    question_text: str
    comment_id: str


class LLMTopic(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: str
    comment_ids: list[str] = Field(default_factory=list)


class LLMCommentAnalysis(BaseModel):
    """What the LLM is asked for: themes + questions + praise/complaint
    topics ONLY.  Sentiment and spam stay deterministic (labelled in
    provenance) so their behaviour is identical and auditable in both
    modes."""

    model_config = ConfigDict(extra="ignore")
    themes: list[LLMTheme] = Field(default_factory=list)
    questions: list[LLMQuestion] = Field(default_factory=list)
    praise_topics: list[LLMTopic] = Field(default_factory=list)
    complaint_topics: list[LLMTopic] = Field(default_factory=list)
