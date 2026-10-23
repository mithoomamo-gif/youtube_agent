"""Comment Analysis Engine — Agent 28, Round 5 (Comments Intelligence).

=====================================================================
TOP-QUESTIONS SEAM (consumed by the Round 5 learning team)
=====================================================================
:func:`top_questions_for_channel` returns the extracted viewer
questions for a channel as a clean list of plain dicts::

    [{"question_text": str,   # VERBATIM sentence from the comment
      "video_id": str | None,
      "comment_id": str,
      "like_count": int}, ...]

ranked by like_count (desc), then reply_count (desc).  Question text
is the viewer's own public comment text quoted for the channel
owner's review — it is stored/returned exactly as extracted, never
paraphrased into a fake quote.  The same items appear, typed, as
``CommentRollup.top_questions``.  Full contract: docs/COMMENTS.md.

=====================================================================
ENGINE RULES (R4-003 honesty law — NO SILENT SUBSTITUTION)
=====================================================================
* Primary: the existing LLM router (``app.providers.llm`` public
  interface — any object with ``async generate(LLMRequest) ->
  LLMResult``), tier T1 (Agent 28's registered default tier), for
  theme clustering + question extraction + praise/complaint topics.
  Router model-registry refusal and pacing are inherited untouched:
  this engine never names a model, it addresses the tier only.
* Fallback: the deterministic analyser
  (:mod:`app.comment_intel.deterministic`) runs ONLY when no LLM is
  configured or the LLM call fails / returns unusable output — and
  the result is then labelled ``provenance.engine='deterministic'``
  with ``provenance.fallback_reason`` naming the failure.  An LLM
  failure NEVER surfaces as an unlabelled swap.
* Sentiment (lexicon) and spam (heuristics) are deterministic in
  BOTH modes this round; provenance labels both methods
  (``sentiment_method`` / ``spam_method``) either way.
* LLM output is validated against the input: theme/topic comment
  ids not in the analysed set are dropped, and LLM questions whose
  text is not verbatim-contained in the cited comment are dropped
  (no fake quotes) — drops are counted in ``rollup.notes``.

Persistence: :mod:`app.comment_intel.storage` (JSON file seam, plus
the ``comment_analyses`` / ``comment_moderation_flags`` tables when
the comments schema worker's tables exist).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import deterministic as det
from .schema import (
    CommentInput,
    CommentRollup,
    LLMCommentAnalysis,
    Provenance,
    SentimentBands,
    ThemeCluster,
    TopicItem,
    TopQuestion,
    comment_from_row,
)

__all__ = [
    "CommentAnalysisEngine",
    "CommentAnalysisError",
    "aggregate_channel_rollup",
    "top_questions_for_channel",
    "MAX_LLM_COMMENTS",
]

log = logging.getLogger("yta.comment_intel")

#: Comments beyond this many are truncated from the LLM prompt (the
#: deterministic layers still cover the full set) and the truncation
#: is recorded in rollup.notes.
MAX_LLM_COMMENTS = 200


class CommentAnalysisError(Exception):
    """Raised in strict mode when the LLM path fails (no fallback)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bands_and_subsets(comments: list[CommentInput], spam_ids: set[str]):
    analysed = [c for c in comments if c.id not in spam_ids]
    counts = {"positive": 0, "neutral": 0, "negative": 0, "question": 0}
    subsets: dict[str, list[CommentInput]] = {"positive": [], "negative": []}
    for c in analysed:
        band = det.classify_comment(c)
        counts[band] += 1
        if band in subsets:
            subsets[band].append(c)
    n = len(analysed)
    bands = SentimentBands(
        **{k: (round(v / n, 4) if n else 0.0) for k, v in counts.items()}
    )
    return analysed, bands, subsets


class CommentAnalysisEngine:
    """Analyse stored comments into per-video / per-channel rollups.

    ``llm`` — the LLM router (or any ``LLMProvider``), or ``None``.
    ``None`` is a legitimate, LABELLED configuration (deterministic
    engine, ``fallback_reason='llm_not_configured'``), not an error.
    ``strict=True`` turns an LLM failure into
    :class:`CommentAnalysisError` instead of a labelled deterministic
    fallback (for callers that would rather retry than degrade).
    """

    def __init__(self, llm=None, *, strict: bool = False) -> None:
        self._llm = llm
        self._strict = strict

    # ------------------------------------------------------------------ API
    async def analyse_video(self, comments, *, video_id=None) -> CommentRollup:
        rows = [comment_from_row(c) for c in comments]
        if video_id is not None:
            for r in rows:
                r.video_id = r.video_id or video_id
        channel_id = next((r.channel_id for r in rows if r.channel_id), None)
        return await self._analyse(rows, scope="video", video_id=video_id, channel_id=channel_id)

    async def analyse_channel(self, comments) -> CommentRollup:
        """Aggregate rollup over all of a channel's comments.

        Computed by analysing per-video groups and aggregating the
        rollups (see :func:`aggregate_channel_rollup`), so a channel
        rollup is always exactly the aggregate of its video rollups.
        """
        rows = [comment_from_row(c) for c in comments]
        channel_id = next((r.channel_id for r in rows if r.channel_id), None)
        by_video: dict[str | None, list[CommentInput]] = {}
        for r in rows:
            by_video.setdefault(r.video_id, []).append(r)
        rollups = []
        for vid, group in by_video.items():
            rollups.append(
                await self._analyse(group, scope="video", video_id=vid, channel_id=channel_id)
            )
        return aggregate_channel_rollup(rollups, channel_id=channel_id)

    # ------------------------------------------------------- store-backed API
    async def analyse_stored_video(self, store, channel_id: str, video_id: str,
                                   *, persist: bool = True):
        """Analyse one video's comments FROM the comments store and
        persist the rollup + spam flags back through it.

        ``store`` is ``app.comments.store.CommentsStore``.  Returns
        ``(rollup, persist_report)`` — the report is the honest
        :func:`app.comment_intel.storage.persist_rollup_to_store`
        dict (``persist_report`` is ``None`` when ``persist=False``).
        """
        rows = store.list_comments(channel_id, video_id)
        rollup = await self.analyse_video(rows, video_id=video_id)
        report = None
        if persist:
            from .storage import persist_rollup_to_store

            report = persist_rollup_to_store(store, rollup)
        return rollup, report

    async def analyse_stored_channel(self, store, channel_id: str,
                                     *, persist: bool = True):
        """Channel rollup over ALL comments the store holds for the
        channel; persisted through the store when ``persist=True``.
        Returns ``(rollup, persist_report)``."""
        rows = store.list_comments(channel_id)
        rollup = await self.analyse_channel(rows)
        report = None
        if persist:
            from .storage import persist_rollup_to_store

            report = persist_rollup_to_store(store, rollup)
        return rollup, report

    # -------------------------------------------------------------- internals
    async def _analyse(self, rows, *, scope, video_id, channel_id) -> CommentRollup:
        flags, spam_ids = det.flag_spam(rows)
        analysed, bands, subsets = _bands_and_subsets(rows, spam_ids)
        notes: list[str] = []

        llm_out: LLMCommentAnalysis | None = None
        model: str | None = None
        fallback_reason: str | None = None

        if self._llm is None:
            fallback_reason = "llm_not_configured"
        else:
            try:
                llm_out, model, llm_notes = await self._llm_layer(analysed)
                notes.extend(llm_notes)
            except Exception as exc:  # LLM failure: label it, never hide it
                if self._strict:
                    raise CommentAnalysisError(
                        f"LLM comment analysis failed: {type(exc).__name__}: {exc}"
                    ) from exc
                fallback_reason = f"{type(exc).__name__}: {exc}"
                log.warning("comment_intel LLM path failed, labelled fallback: %s", fallback_reason)

        if llm_out is not None:
            themes, questions, praise, complaints = self._validate_llm_layer(
                llm_out, analysed, subsets, notes
            )
            provenance = Provenance(engine="llm", model=model, generated_at=_now_iso())
        else:
            themes = det.token_themes(analysed)
            questions = det.extract_questions(analysed)
            praise = det.topic_items(subsets["positive"])
            complaints = det.topic_items(subsets["negative"])
            provenance = Provenance(
                engine="deterministic",
                model=None,
                generated_at=_now_iso(),
                fallback_reason=fallback_reason,
            )

        return CommentRollup(
            scope=scope,
            video_id=video_id,
            channel_id=channel_id,
            comment_count=len(rows),
            analysed_count=len(analysed),
            spam_count=len(spam_ids),
            sentiment=bands,
            themes=themes,
            top_questions=questions,
            praise_topics=praise,
            complaint_topics=complaints,
            spam_flags=flags,
            provenance=provenance,
            notes=notes,
        )

    async def _llm_layer(self, analysed: list[CommentInput]):
        from app.providers.llm.models import LLMRequest, Tier

        prompt_comments = analysed[:MAX_LLM_COMMENTS]
        notes: list[str] = []
        if len(analysed) > MAX_LLM_COMMENTS:
            notes.append(
                f"llm_prompt_truncated: {len(analysed) - MAX_LLM_COMMENTS} comments "
                "beyond MAX_LLM_COMMENTS were covered by deterministic layers only"
            )
        listing = "\n".join(
            f"[{c.id}] (likes={c.like_count or 0}) {c.text}" for c in prompt_comments
        )
        req = LLMRequest(
            tier=Tier.T1,
            agent_id="28",
            task="comments.analysis",
            channel_id=next((c.channel_id for c in analysed if c.channel_id), None),
            video_id=next((c.video_id for c in analysed if c.video_id), None),
            output_schema=LLMCommentAnalysis,
            max_output_tokens=4096,
            temperature=0.2,
            prompt=(
                "You are the Comment Intelligence analyst for a YouTube channel.\n"
                "Analyse the viewer comments below. Return ONLY:\n"
                "- themes: clusters of comments about the same subject "
                "(label + the comment ids in each cluster);\n"
                "- questions: viewer questions — question_text MUST be the "
                "verbatim question sentence copied from the cited comment, "
                "never paraphrased;\n"
                "- praise_topics / complaint_topics: what viewers praise / "
                "complain about (label + comment ids).\n"
                "Use only comment ids from the list. Comments:\n" + listing
            ),
        )
        result = await self._llm.generate(req)
        parsed = getattr(result, "parsed", None)
        if parsed is None:
            # Adapters that return raw text: parse the JSON ourselves.
            import json

            parsed = LLMCommentAnalysis(**json.loads(result.text))
        if not isinstance(parsed, LLMCommentAnalysis):
            parsed = LLMCommentAnalysis(**parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(parsed, LLMCommentAnalysis):
            raise CommentAnalysisError(f"LLM returned unusable output: {type(parsed).__name__}")
        return parsed, (getattr(result, "model", None) or None), notes

    @staticmethod
    def _validate_llm_layer(llm_out, analysed, subsets, notes):
        valid_ids = {c.id for c in analysed}
        by_id = {c.id: c for c in analysed}
        dropped_ids = 0
        dropped_questions = 0

        def clean_topics(items):
            out = []
            nonlocal dropped_ids
            for item in items:
                ids = [i for i in item.comment_ids if i in valid_ids]
                dropped_ids += len(item.comment_ids) - len(ids)
                if ids:
                    out.append(TopicItem(label=item.label, comment_ids=ids, count=len(ids)))
            return out

        themes: list[ThemeCluster] = []
        for t in llm_out.themes:
            ids = [i for i in t.comment_ids if i in valid_ids]
            dropped_ids += len(t.comment_ids) - len(ids)
            if ids:
                themes.append(
                    ThemeCluster(
                        label=t.label,
                        comment_ids=ids,
                        share=round(len(ids) / len(analysed), 4) if analysed else 0.0,
                    )
                )

        questions: list[TopQuestion] = []
        seen_q: set[tuple[str, str]] = set()
        for q in llm_out.questions:
            comment = by_id.get(q.comment_id)
            if comment is None:
                dropped_questions += 1
                continue
            # No fake quotes: the question must be verbatim-contained
            # in the cited comment (whitespace/case-normalised).
            norm_text = " ".join((comment.text or "").lower().split())
            norm_q = " ".join(q.question_text.lower().split()).rstrip("?").strip()
            if not norm_q or norm_q not in norm_text:
                dropped_questions += 1
                continue
            key = (q.comment_id, norm_q)
            if key in seen_q:
                continue
            seen_q.add(key)
            questions.append(
                TopQuestion(
                    question_text=q.question_text,
                    video_id=comment.video_id,
                    comment_id=q.comment_id,
                    like_count=comment.like_count or 0,
                )
            )
        questions.sort(key=lambda q: (-(q.like_count or 0), q.comment_id))
        if dropped_ids:
            notes.append(f"llm_unknown_comment_ids_dropped: {dropped_ids}")
        if dropped_questions:
            notes.append(f"llm_non_verbatim_questions_dropped: {dropped_questions}")
        return themes, questions, clean_topics(llm_out.praise_topics), clean_topics(llm_out.complaint_topics)


def aggregate_channel_rollup(rollups: list[CommentRollup], *, channel_id=None) -> CommentRollup:
    """Aggregate per-video rollups into one per-channel rollup.

    Bands are recomputed from summed counts (weighted by analysed
    counts); themes/topics merge by label; questions merge and
    re-rank by likes.  Provenance: a channel rollup is 'llm' only
    when EVERY contributing video rollup was LLM-produced by the
    same model; otherwise it is labelled 'deterministic' with a
    fallback_reason explaining the mix — never silently upgraded.
    """
    rollups = list(rollups)
    total = sum(r.comment_count for r in rollups)
    analysed_n = sum(r.analysed_count for r in rollups)
    spam_n = sum(r.spam_count for r in rollups)

    def share(count_key: str) -> float:
        if not analysed_n:
            return 0.0
        weighted = sum(getattr(r.sentiment, count_key) * r.analysed_count for r in rollups)
        return round(weighted / analysed_n, 4)

    bands = SentimentBands(
        positive=share("positive"), neutral=share("neutral"),
        negative=share("negative"), question=share("question"),
    )

    def merge_themes() -> list[ThemeCluster]:
        merged: dict[str, list[str]] = {}
        for r in rollups:
            for t in r.themes:
                merged.setdefault(t.label, []).extend(t.comment_ids)
        out = [
            ThemeCluster(label=label, comment_ids=ids,
                         share=round(len(ids) / analysed_n, 4) if analysed_n else 0.0)
            for label, ids in merged.items()
        ]
        return sorted(out, key=lambda t: (-len(t.comment_ids), t.label))

    def merge_topics(attr: str) -> list[TopicItem]:
        merged: dict[str, list[str]] = {}
        for r in rollups:
            for item in getattr(r, attr):
                merged.setdefault(item.label, []).extend(item.comment_ids)
        out = [TopicItem(label=l, comment_ids=ids, count=len(ids)) for l, ids in merged.items()]
        return sorted(out, key=lambda i: (-i.count, i.label))

    questions = sorted(
        (q for r in rollups for q in r.top_questions),
        key=lambda q: (-(q.like_count or 0), q.comment_id),
    )
    flags = [f for r in rollups for f in r.spam_flags]
    notes = [n for r in rollups for n in r.notes]

    engines = {r.provenance.engine for r in rollups}
    models = {r.provenance.model for r in rollups}
    if rollups and engines == {"llm"} and len(models) == 1:
        provenance = Provenance(engine="llm", model=models.pop(), generated_at=_now_iso())
    else:
        reason = None
        if rollups:
            reasons = {r.provenance.fallback_reason for r in rollups if r.provenance.fallback_reason}
            reason = (
                "channel aggregate mixes engines/models: "
                f"engines={sorted(engines)}, models={sorted(str(m) for m in models)}"
                + (f"; video fallbacks: {sorted(reasons)}" if reasons else "")
            )
        provenance = Provenance(
            engine="deterministic", model=None, generated_at=_now_iso(),
            fallback_reason=reason,
        )

    return CommentRollup(
        scope="channel",
        video_id=None,
        channel_id=channel_id or next((r.channel_id for r in rollups if r.channel_id), None),
        comment_count=total,
        analysed_count=analysed_n,
        spam_count=spam_n,
        sentiment=bands,
        themes=merge_themes(),
        top_questions=questions,
        praise_topics=merge_topics("praise_topics"),
        complaint_topics=merge_topics("complaint_topics"),
        spam_flags=flags,
        provenance=provenance,
        notes=notes,
    )


def top_questions_for_channel(rollups_or_rollup) -> list[dict]:
    """TOP-QUESTIONS SEAM — see module docstring / docs/COMMENTS.md.

    Accepts one channel :class:`CommentRollup` (or a list of per-video
    rollups, aggregated first) and returns::

        [{"question_text": str, "video_id": str | None,
          "comment_id": str, "like_count": int}, ...]

    ranked by like_count desc.  ``question_text`` is the verbatim
    viewer question, exactly as extracted.
    """
    if isinstance(rollups_or_rollup, CommentRollup):
        rollup = rollups_or_rollup
    else:
        rollup = aggregate_channel_rollup(list(rollups_or_rollup))
    return [q.model_dump() for q in rollup.top_questions]
