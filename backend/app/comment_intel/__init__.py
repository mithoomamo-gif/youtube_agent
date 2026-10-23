"""Comment Intelligence — Agent 28, Round 5.

Analyses stored viewer comments into per-video and per-channel
rollups: sentiment bands (positive / neutral / negative / question),
theme clusters, top viewer questions, praise & complaint topics and
spam/abuse flags for the moderation queue.

Primary engine: the LLM router (tier T1).  Fallback: a deterministic
lexicon/pattern analyser — ALWAYS labelled as such in the rollup's
``provenance`` (R4-003: no silent substitution; see
:mod:`app.comment_intel.engine` and docs/COMMENTS.md).

The learning team consumes viewer questions through
:func:`app.comment_intel.engine.top_questions_for_channel`.
"""

from __future__ import annotations

from .engine import (
    CommentAnalysisEngine,
    CommentAnalysisError,
    aggregate_channel_rollup,
    top_questions_for_channel,
)
from .schema import (
    CommentInput,
    CommentRollup,
    Provenance,
    SentimentBands,
    SpamFlag,
    ThemeCluster,
    TopQuestion,
    TopicItem,
    comment_from_row,
)
from .storage import (
    CommentAnalysisStore,
    persist_rollup_to_db,
    persist_rollup_to_store,
)

__all__ = [
    "CommentAnalysisEngine",
    "CommentAnalysisError",
    "aggregate_channel_rollup",
    "top_questions_for_channel",
    "CommentInput",
    "CommentRollup",
    "Provenance",
    "SentimentBands",
    "SpamFlag",
    "ThemeCluster",
    "TopQuestion",
    "TopicItem",
    "comment_from_row",
    "CommentAnalysisStore",
    "persist_rollup_to_db",
    "persist_rollup_to_store",
]
