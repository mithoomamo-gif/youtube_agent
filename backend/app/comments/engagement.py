"""Comment Engagement service — Agent 28, Round 5 (ENGAGEMENT half).

This module owns the *human-review* side of Comments Intelligence:

1. **Draft reply generation** — for one stored comment, produce a
   suggested reply and persist it as a ``proposed`` draft via
   :class:`app.comments.store.CommentsStore` (``comment_drafts``).
2. **Approval state machine** — ``proposed`` -> ``approved`` |
   ``rejected`` | ``discarded`` (all terminal; ``discarded`` is
   distinct from ``rejected``), delegated to the store's guarded
   transitions; this service adds NO transitions of its own.
   A ``proposed`` draft's text may also be EDITED
   (:meth:`CommentEngagementService.edit_draft`), with the edit
   recorded in the draft's provenance by the store.
3. **Moderation queue** — list the analysis worker's flags from
   ``comment_moderation_flags`` and record HUMAN decisions locally
   (dismiss / mark-reviewed).

=====================================================================
THE NOT-WIRED POSTING BOUNDARY (read this before touching approvals)
=====================================================================
**Approving a draft does NOT post it. Nothing in this module — or
anywhere in Round 5 — posts a reply.**  There is no posting
implementation in this round at all: approval only flips the local
draft row to ``status='approved'`` (with ``decided_at`` /
``decided_by``).  A future round may wire posting for connected
channels behind this exact gate; until then every draft view this
service returns carries::

    "posted": False, "posting": "not_wired"

and every moderation-queue view carries ``"action_executed": False``.
Suggested moderation actions (``suggested_action`` /
``suggested_action_label``) are LABELS for a human reviewer — they
are never executed against YouTube, here or anywhere else.

NO-POST GUARANTEE (implementation half)
---------------------------------------
Draft generation and approval are purely local operations: the only
outbound call this module can ever make is the LLM router call for
draft *text generation* (through ``app.providers.llm``).  No code
path here calls any YouTube write endpoint — the words for such
endpoints deliberately never appear in this file, so a grep audit of
the comments feature finds zero write-endpoint strings in the
engagement code.

Draft generation + provenance (R4-003 honesty law)
--------------------------------------------------
* Primary: the existing LLM router (any object with
  ``async generate(LLMRequest) -> LLMResult``), tier **T1** (Agent
  28's registered default tier), task ``comments.reply_draft``.  The
  router's registry refusal / pacing / budget caps are inherited
  untouched; this service never names a model.
* Fallback: deterministic reply templates (below) run ONLY when no
  LLM is configured or the LLM call fails / returns unusable output
  (empty, over-long, or containing a link).  The draft's provenance
  then records ``engine='deterministic'`` and a ``fallback_reason``
  naming the failure — a fallback is never dressed up as an LLM
  result.  ``strict=True`` turns an LLM failure into
  :class:`DraftGenerationError` instead of a labelled fallback.
* Provenance is stored on the draft row (``provenance_json``):
  ``{engine, model, language, script, tone, fallback_reason,
  generated_at, prompt_version}``.

Tone + language
---------------
Draft tone is **helpful and simple** (``tone='helpful_simple_v1'``):
short, warm, no links, no promises the channel cannot keep (the
deterministic templates only thank / acknowledge / say a topic may
be covered in a future video).  The reply language follows the
COMMENT's language, inferred by :func:`detect_language`:

* Arabic script (U+0600–U+06FF) -> Urdu (``language='ur'``,
  ``script='arabic'``) — the channels this app serves are
  Urdu-first, so Arabic-script comments are answered in Urdu.
* Devanagari (U+0900–U+097F) -> Hindi (``'hi'``).
* Latin script with >= 2 Roman-Urdu marker words (hai, hain, ka,
  ki, mein, aap, bohat, acha, kese, kya, ...) -> Roman Urdu
  (``language='ur'``, ``script='roman'``).
* otherwise -> English (``'en'``).

This is a documented heuristic, not a language model; the detected
language is recorded in provenance so a reviewer can see why a
draft reads the way it does.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.comments.store import (
    CommentsNotFoundError,
    CommentsScopeError,
    CommentsStateError,
    CommentsStore,
)

__all__ = [
    "POSTING_STATUS",
    "DRAFT_TONE",
    "PROMPT_VERSION",
    "EngagementError",
    "DraftGenerationError",
    "IllegalTransitionError",
    "detect_language",
    "classify_comment_kind",
    "deterministic_reply",
    "draft_view",
    "moderation_flag_view",
    "CommentEngagementService",
]

log = logging.getLogger("yta.comments.engagement")

#: The posting boundary, stamped on every draft view.  Approval is a
#: LOCAL decision only; posting is NOT WIRED in this round.
POSTING_STATUS = "not_wired"

#: Draft tone label recorded in provenance.
DRAFT_TONE = "helpful_simple_v1"

#: Prompt identity recorded in provenance for LLM-generated drafts.
PROMPT_VERSION = "comments.reply_draft.v1"

#: Agent 28's registered default tier (app.agents.registry).
DRAFT_TIER = "T1"

#: An LLM reply longer than this is treated as unusable (a comment
#: reply should be short) and the labelled fallback takes over.
MAX_DRAFT_CHARS = 600


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EngagementError(Exception):
    """Base class for engagement-service errors."""


class DraftGenerationError(EngagementError):
    """Strict mode: the LLM path failed and no fallback was allowed."""


class IllegalTransitionError(EngagementError, ValueError):
    """A guarded state-machine transition was attempted illegally
    (e.g. deciding a draft that is already in a terminal state)."""


# ---------------------------------------------------------------------------
# Language + kind inference (documented heuristics — see module docstring)
# ---------------------------------------------------------------------------

_ROMAN_URDU_MARKERS = {
    "hai", "hain", "ka", "ki", "ke", "ko", "mein", "mai", "aap",
    "bohat", "boht", "acha", "accha", "kese", "kaisay", "kya", "kyun",
    "bhai", "yar", "yaar", "shukriya", "zabardast", "maza", "agaya",
    "samajh", "laga", "raha", "rahi", "hoga", "chahiye", "bhi", "nahi",
    "nhi", "aur", "yeh", "woh", "apka", "apki", "hamari",
}

_QUESTION_MARKERS = {
    "?", "how", "what", "why", "when", "where", "which", "can",
    "could", "please explain", "kya", "kese", "kaisay", "kyun", "kab",
}

_PRAISE_WORDS = {
    "great", "amazing", "awesome", "love", "loved", "best", "superb",
    "excellent", "fantastic", "wonderful", "beautiful", "zabardast",
    "shukriya", "thanks", "thank",
}

_COMPLAINT_WORDS = {
    "bad", "worst", "boring", "hate", "disappointed", "disappointing",
    "wrong", "poor", "useless", "waste", "bekar",
}


def detect_language(text: str) -> tuple[str, str]:
    """Infer ``(language, script)`` for a comment — see module docstring.

    Returns one of ``('ur', 'arabic')``, ``('hi', 'devanagari')``,
    ``('ur', 'roman')`` or ``('en', 'latin')``.
    """
    body = text or ""
    if any("؀" <= ch <= "ۿ" for ch in body):
        return ("ur", "arabic")
    if any("ऀ" <= ch <= "ॿ" for ch in body):
        return ("hi", "devanagari")
    tokens = re.findall(r"[a-z]+", body.lower())
    hits = sum(1 for t in tokens if t in _ROMAN_URDU_MARKERS)
    if hits >= 2:
        return ("ur", "roman")
    return ("en", "latin")


def classify_comment_kind(text: str) -> str:
    """Bucket a comment for template selection:
    ``'question' | 'praise' | 'complaint' | 'generic'``."""
    body = (text or "").lower()
    tokens = set(re.findall(r"[a-z]+", body))
    if "?" in body or tokens & _QUESTION_MARKERS:
        return "question"
    if tokens & _COMPLAINT_WORDS:
        return "complaint"
    if tokens & _PRAISE_WORDS:
        return "praise"
    return "generic"


# ---------------------------------------------------------------------------
# Deterministic fallback templates (labelled engine='deterministic')
#
# Deliberately generic acknowledgements: thank the viewer, and for a
# question say only that we will TRY to cover it in a future video.
# They make no factual claims and no promises, so they are safe for a
# human reviewer to approve as-is or edit.
# ---------------------------------------------------------------------------

_TEMPLATES: dict[tuple[str, str], dict[str, str]] = {
    ("en", "latin"): {
        "question": (
            "Thank you for your question! We will try to cover this "
            "in an upcoming video — stay tuned."
        ),
        "praise": (
            "Thank you so much for the kind words! It means a lot — "
            "glad you enjoyed the video."
        ),
        "complaint": (
            "Thank you for the honest feedback — we are sorry this "
            "one missed the mark. We will keep your point in mind "
            "for the next videos."
        ),
        "generic": (
            "Thanks for watching and for your comment! More videos "
            "are on the way."
        ),
    },
    ("ur", "arabic"): {
        "question": (
            "آپ کے سوال کا بہت شکریہ! ہم کوشش کریں گے کہ آنے والی "
            "ویڈیو میں اس کا جواب دیں۔"
        ),
        "praise": (
            "آپ کی محبت اور حوصلہ افزائی کا بہت شکریہ! خوشی ہوئی کہ "
            "آپ کو ویڈیو پسند آئی۔"
        ),
        "complaint": (
            "آپ کی رائے کے لیے شکریہ، اور معذرت کہ یہ ویڈیو آپ کی "
            "امید پر پوری نہیں اتری۔ ہم آپ کی بات کو اگلی ویڈیوز میں "
            "ذہن میں رکھیں گے۔"
        ),
        "generic": (
            "ویڈیو دیکھنے اور تبصرہ کرنے کا شکریہ! مزید ویڈیوز جلد "
            "آ رہی ہیں۔"
        ),
    },
    ("ur", "roman"): {
        "question": (
            "Aap ke sawal ka bohat shukriya! Hum koshish karenge ke "
            "aanay wali video mein is ka jawab dein."
        ),
        "praise": (
            "Aap ki muhabbat aur hosla afzai ka bohat shukriya! "
            "Khushi hui ke aap ko video pasand aayi."
        ),
        "complaint": (
            "Aap ki raaye ke liye shukriya, aur mazrat ke yeh video "
            "aap ki umeed par poori nahi utri. Hum aap ki baat ko "
            "agli videos mein zehen mein rakhenge."
        ),
        "generic": (
            "Video dekhne aur comment karne ka shukriya! Mazeed "
            "videos jald aa rahi hain."
        ),
    },
    ("hi", "devanagari"): {
        "question": (
            "आपके सवाल के लिए धन्यवाद! हम कोशिश करेंगे कि आने वाली "
            "वीडियो में इसका जवाब दें।"
        ),
        "praise": (
            "आपके प्यारे शब्दों के लिए बहुत धन्यवाद! खुशी हुई कि "
            "आपको वीडियो पसंद आया।"
        ),
        "complaint": (
            "आपकी राय के लिए धन्यवाद, और खेद है कि यह वीडियो आपकी "
            "उम्मीद पर खरा नहीं उतरा। हम आपकी बात को अगली वीडियों "
            "में ध्यान में रखेंगे।"
        ),
        "generic": (
            "वीडियो देखने और टिप्पणी करने के लिए धन्यवाद! और "
            "वीडियो जल्द आ रहे हैं।"
        ),
    },
}


def deterministic_reply(comment_text: str, language: str, script: str,
                        kind: Optional[str] = None) -> str:
    """The deterministic fallback reply for a comment (labelled
    ``engine='deterministic'`` in provenance by the caller)."""
    kind = kind or classify_comment_kind(comment_text)
    table = _TEMPLATES.get((language, script)) or _TEMPLATES[("en", "latin")]
    return table.get(kind) or table["generic"]


# ---------------------------------------------------------------------------
# Views — the not-wired posting boundary is stamped HERE, on every dict
# that leaves the service, so no consumer can mistake approval for posting.
# ---------------------------------------------------------------------------


def draft_view(row: dict) -> dict:
    """Shape a stored draft row for API/UI consumers.

    Carries BOTH ``text`` (the documented contract field) and the
    store's ``draft_text``, the parsed ``provenance``, and the
    posting boundary: ``posted`` is always ``False`` and ``posting``
    always ``'not_wired'`` in this round — approving a draft does
    NOT post it.
    """
    view = dict(row)
    view["text"] = row.get("draft_text")
    view["posted"] = False
    view["posting"] = POSTING_STATUS
    return view


#: Suggested-action LABELS for the moderation queue, by flag kind,
#: when the analysis worker did not store a more specific label.
#: Labels only — nothing here is ever executed.
_DEFAULT_ACTION_LABELS = {
    "spam": "review_for_spam",
    "abuse": "hide_recommended",
    "other": "review",
}


def moderation_flag_view(row: dict, comment: Optional[dict] = None) -> dict:
    """Shape a stored moderation flag for the review queue.

    ``suggested_action_label`` is a LABEL for the human reviewer
    only; ``action_executed`` is always ``False`` and
    ``youtube_action`` always ``None`` — recording a decision in
    this queue never performs any action against YouTube.
    """
    view = dict(row)
    stored = row.get("suggested_action")
    view["suggested_action_label"] = (
        stored if stored else _DEFAULT_ACTION_LABELS.get(
            row.get("flag_kind"), "review")
    )
    view["action_is_label_only"] = True
    view["action_executed"] = False
    view["youtube_action"] = None
    if comment is not None:
        view["comment_text"] = comment.get("text")
        view["comment_author"] = comment.get("author_display_name")
    return view


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_llm_reply(text: Any) -> Optional[str]:
    """Validate an LLM reply; return the cleaned text or ``None``
    (unusable -> labelled deterministic fallback)."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip().strip('"').strip()
    # Strip a markdown fence if the model wrapped the reply in one.
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
    if not cleaned or len(cleaned) > MAX_DRAFT_CHARS:
        return None
    lowered = cleaned.lower()
    if "http://" in lowered or "https://" in lowered or "www." in lowered:
        return None  # replies never carry links (tone contract)
    return cleaned


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CommentEngagementService:
    """Draft generation + approval + moderation queue (all local).

    ``store`` — a :class:`CommentsStore`.  ``llm`` — the LLM router
    (or any ``LLMProvider``), or ``None`` (a legitimate, LABELLED
    configuration: deterministic drafts with
    ``fallback_reason='llm_not_configured'``).  ``strict=True``
    turns an LLM failure into :class:`DraftGenerationError` instead
    of a labelled fallback.
    """

    def __init__(self, store: CommentsStore, llm: Any = None, *,
                 strict: bool = False) -> None:
        self._store = store
        self._llm = llm
        self._strict = strict

    # ------------------------------------------------------------------
    # 1. Draft generation
    # ------------------------------------------------------------------
    async def generate_draft(self, comment_id: Any, *,
                             channel_id: Optional[str] = None) -> dict:
        """Generate a suggested reply for one stored comment and
        persist it as a ``proposed`` draft; return the draft view.

        Raises :class:`CommentsNotFoundError` for an unknown comment
        and :class:`CommentsScopeError` when ``channel_id`` disagrees
        with the comment's owner channel.  The returned draft is a
        LOCAL row only — ``posted`` is ``False`` and ``posting`` is
        ``'not_wired'`` (see the module docstring's boundary).
        """
        comment = self._store.get_comment(comment_id, channel_id=channel_id)
        if comment is None:
            # Distinguish "unknown" from "another channel's" the way
            # the store's writes do: an unscoped hit means scope error.
            if self._store.get_comment(comment_id) is not None:
                raise CommentsScopeError(
                    f"comment {comment_id} belongs to a different channel"
                )
            raise CommentsNotFoundError(
                f"comment {comment_id} does not exist"
            )

        language, script = detect_language(comment.get("text") or "")
        kind = classify_comment_kind(comment.get("text") or "")

        text: Optional[str] = None
        provenance: dict[str, Any] = {
            "engine": "deterministic",
            "model": None,
            "language": language,
            "script": script,
            "comment_kind": kind,
            "tone": DRAFT_TONE,
            "fallback_reason": None,
            "generated_at": _now_iso(),
            "prompt_version": PROMPT_VERSION,
        }

        if self._llm is None:
            provenance["fallback_reason"] = "llm_not_configured"
        else:
            try:
                llm_text, model = await self._llm_reply(comment, language, script)
            except Exception as exc:  # LLM failure: label it, never hide it
                if self._strict:
                    raise DraftGenerationError(
                        f"LLM draft generation failed: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                provenance["fallback_reason"] = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "comment draft LLM path failed, labelled fallback: %s",
                    provenance["fallback_reason"],
                )
            else:
                if llm_text is not None:
                    text = llm_text
                    provenance["engine"] = "llm"
                    provenance["model"] = model
                else:
                    provenance["fallback_reason"] = "llm_output_unusable"
                    if self._strict:
                        raise DraftGenerationError(
                            "LLM draft generation returned unusable output"
                        )

        if text is None:
            text = deterministic_reply(
                comment.get("text") or "", language, script, kind
            )

        row = self._store.create_draft(
            comment["id"],
            text,
            channel_id=comment["channel_id"],
            created_by="agent28",
            provenance=provenance,
        )
        return draft_view(row)

    async def _llm_reply(self, comment: MappingLike, language: str,
                         script: str) -> tuple[Optional[str], Optional[str]]:
        from app.providers.llm.models import LLMRequest, Tier

        author = comment.get("author_display_name") or "a viewer"
        req = LLMRequest(
            tier=Tier(DRAFT_TIER),
            agent_id="28",
            task="comments.reply_draft",
            channel_id=comment.get("channel_id"),
            video_id=comment.get("video_id"),
            prompt_version=PROMPT_VERSION,
            max_output_tokens=256,
            temperature=0.4,
            prompt=(
                "You write comment replies for a YouTube channel "
                "(Agent 28, Comment Engagement).\n"
                "Write ONE short, helpful, friendly reply to the "
                "viewer comment below.\n"
                "Rules:\n"
                f"- Reply in the SAME language and script as the "
                f"comment (detected: language={language}, "
                f"script={script}).\n"
                "- Keep it simple and warm: at most 2 short sentences.\n"
                "- No links, no hashtags, no markdown.\n"
                "- Never promise a specific future video or date; "
                "you may say the topic may be covered in a future "
                "video.\n"
                "- Return ONLY the reply text.\n"
                f"Viewer ({author}) commented:\n"
                f"{comment.get('text') or ''}"
            ),
        )
        result = await self._llm.generate(req)
        return _clean_llm_reply(getattr(result, "text", None)), (
            getattr(result, "model", None) or None
        )

    # ------------------------------------------------------------------
    # 2. Approval state machine — LOCAL ONLY.  Approving a draft does
    #    NOT post it; posting is not wired in this round (see the
    #    module docstring).  These methods only flip the local row.
    # ------------------------------------------------------------------
    def edit_draft(self, draft_id: Any, draft_text: str, *,
                   channel_id: Optional[str] = None,
                   edited_by: Optional[str] = None) -> dict:
        """Edit a ``proposed`` draft's text; return the draft view.

        Guarded by the store: editing a terminal draft
        (``approved`` / ``rejected`` / ``discarded``) raises
        :class:`IllegalTransitionError`; an unknown draft raises
        :class:`CommentsNotFoundError`; a cross-channel edit raises
        :class:`CommentsScopeError`.  The edit (who / when / previous
        and new text) is recorded in the draft's provenance by the
        store.  ``edited_by`` is an optional free-text actor label,
        mirroring ``decided_by``.

        THIS NEVER POSTS ANYTHING — it only rewrites the local
        draft row's text; the view carries ``posted=False`` /
        ``posting='not_wired'``.
        """
        try:
            row = self._store.update_draft_text(
                draft_id, draft_text,
                channel_id=channel_id, edited_by=edited_by,
            )
        except CommentsStateError as exc:
            raise IllegalTransitionError(str(exc)) from exc
        if row is None:
            raise CommentsNotFoundError(f"draft {draft_id} does not exist")
        return draft_view(row)

    def decide_draft(self, draft_id: Any, decision: str, *,
                       channel_id: Optional[str] = None,
                       decided_by: Optional[str] = None) -> dict:
        """Apply a human decision (``'approved'`` | ``'rejected'`` |
        ``'discarded'``) to a ``proposed`` draft; return the draft
        view.  ``discarded`` is a terminal state DISTINCT from
        ``rejected``: the reviewer throws the draft away without
        deciding against its content.

        Guarded by the store: deciding an already-decided draft
        raises :class:`IllegalTransitionError` (terminal states are
        immutable); an unknown draft raises
        :class:`CommentsNotFoundError`; a cross-channel decision
        raises :class:`CommentsScopeError`.  ``decided_by`` records
        WHO decided when the caller supplies an actor label (the
        codebase has no authenticated-actor concept in this round,
        so it is an optional free-text label); ``decided_at`` is
        always stamped by the store.

        THIS NEVER POSTS ANYTHING.  The returned view carries
        ``posted=False`` / ``posting='not_wired'``.
        """
        if decision not in ("approved", "rejected", "discarded"):
            raise ValueError(
                f"draft decision must be 'approved', 'rejected' or "
                f"'discarded', got {decision!r}"
            )
        try:
            row = self._store.set_draft_status(
                draft_id, decision,
                channel_id=channel_id, decided_by=decided_by,
            )
        except CommentsStateError as exc:
            raise IllegalTransitionError(str(exc)) from exc
        if row is None:
            raise CommentsNotFoundError(f"draft {draft_id} does not exist")
        return draft_view(row)

    def approve_draft(self, draft_id: Any, **kwargs: Any) -> dict:
        """Mark a proposed draft ``approved`` — a LOCAL decision
        only.  This does NOT post the reply (posting is not wired
        in this round; see the module docstring)."""
        return self.decide_draft(draft_id, "approved", **kwargs)

    def reject_draft(self, draft_id: Any, **kwargs: Any) -> dict:
        """Mark a proposed draft ``rejected`` (terminal, local only)."""
        return self.decide_draft(draft_id, "rejected", **kwargs)

    def discard_draft(self, draft_id: Any, **kwargs: Any) -> dict:
        """Mark a proposed draft ``discarded`` — a terminal state
        DISTINCT from ``rejected`` (local only, never posted)."""
        return self.decide_draft(draft_id, "discarded", **kwargs)

    def get_draft(self, draft_id: Any, *,
                  channel_id: Optional[str] = None) -> dict:
        """One draft view, or :class:`CommentsNotFoundError`
        (cross-channel reads as not-found, matching the store)."""
        row = self._store.get_draft(draft_id, channel_id=channel_id)
        if row is None:
            raise CommentsNotFoundError(f"draft {draft_id} does not exist")
        return draft_view(row)

    def list_drafts(self, channel_id: str, *,
                    status: Optional[str] = None,
                    comment_id: Optional[Any] = None,
                    video_id: Optional[str] = None,
                    limit: Optional[int] = None) -> list[dict]:
        """Draft views owned by ``channel_id`` (always scoped)."""
        rows = self._store.list_drafts(
            channel_id, status=status, comment_id=comment_id,
            video_id=video_id, limit=limit,
        )
        return [draft_view(r) for r in rows]

    # ------------------------------------------------------------------
    # 3. Moderation queue — labels + LOCAL human decisions only.
    #    Recording a decision never executes any action against
    #    YouTube (``action_executed`` is always False in the views).
    # ------------------------------------------------------------------
    def list_moderation_queue(self, channel_id: str, *,
                              status: Optional[str] = "pending",
                              flag_kind: Optional[str] = None,
                              video_id: Optional[str] = None,
                              limit: Optional[int] = None) -> list[dict]:
        """Flag views for the human review queue, scoped to
        ``channel_id``.  Defaults to ``status='pending'`` (the
        actionable queue); pass ``status=None`` for all flags.
        Each view is enriched with the flagged comment's text/author
        and its suggested-action LABEL."""
        rows = self._store.list_moderation_flags(
            channel_id, status=status, flag_kind=flag_kind,
            video_id=video_id, limit=limit,
        )
        views = []
        for row in rows:
            comment = self._store.get_comment(
                row["comment_id"], channel_id=channel_id
            )
            views.append(moderation_flag_view(row, comment))
        return views

    def decide_moderation_flag(self, flag_id: Any, decision: str, *,
                               channel_id: Optional[str] = None,
                               decided_by: Optional[str] = None) -> dict:
        """Record a LOCAL human decision on a pending flag.

        ``decision`` is ``'dismiss'`` (the flag needs no action ->
        stored ``status='dismissed'``) or ``'mark_reviewed'`` (a
        human reviewed the comment and handled it outside this
        queue -> stored ``status='actioned'``; the word records the
        HUMAN's review, it does not mean this service performed any
        action — ``action_executed`` in the returned view is always
        ``False``).  Deciding an already-decided flag raises
        :class:`IllegalTransitionError`; an unknown flag raises
        :class:`CommentsNotFoundError`.
        """
        mapping = {"dismiss": "dismissed", "mark_reviewed": "actioned"}
        if decision not in mapping:
            raise ValueError(
                f"moderation decision must be one of "
                f"{sorted(mapping)}, got {decision!r}"
            )
        try:
            row = self._store.set_moderation_flag_status(
                flag_id, mapping[decision],
                channel_id=channel_id, decided_by=decided_by,
            )
        except CommentsStateError as exc:
            raise IllegalTransitionError(str(exc)) from exc
        if row is None:
            raise CommentsNotFoundError(
                f"moderation flag {flag_id} does not exist"
            )
        view = moderation_flag_view(row)
        view["decision"] = decision
        return view


#: Type alias kept loose (comment rows are plain dicts from the store).
MappingLike = Any


# ---------------------------------------------------------------------------
# API-facing plumbing (used by app.routers.comments)
#
# Mirrors app.style_lab.service: channel references resolve to the
# internal channels.id (a YouTube channel id is also accepted),
# unknown owner -> 404 channel_not_found, cross-channel rows -> 404.
# The default LLM is built lazily through the existing Gemini router
# factory ONLY when a Gemini key actually resolves; anything else is
# the labelled deterministic configuration.
# ---------------------------------------------------------------------------

_default_llm_cache: dict[str, Any] = {"resolved": False, "llm": None}


def _conn_factory():
    from app import db as db_module

    return db_module.get_connection


def default_store() -> CommentsStore:
    return CommentsStore(_conn_factory())


def default_llm() -> Any:
    """The process-default LLM router for draft generation, or None.

    Built via ``app.integration.content.build_gemini_router`` with
    the shared secret provider, and only when a Gemini key resolves
    (``gemini_key_available``).  Any failure -> ``None`` (the
    labelled deterministic configuration).  Resolved once per
    process; tests inject their own service instead.
    """
    if _default_llm_cache["resolved"]:
        return _default_llm_cache["llm"]
    llm = None
    try:
        import asyncio

        from app.integration.content import (
            build_gemini_router,
            gemini_key_available,
        )
        from app.providers.secrets_env import create_secret_provider

        provider = create_secret_provider()

        async def _check() -> bool:
            return await gemini_key_available(provider)

        try:
            available = asyncio.run(_check())
        except RuntimeError:
            # Already inside a running loop (the router path): build
            # the router optimistically — the adapter resolves the
            # key per-call and any failure surfaces as a labelled
            # deterministic fallback at generation time.
            available = True
        if available:
            llm = build_gemini_router(provider)
    except Exception as exc:  # noqa: BLE001 — degrade to labelled fallback
        log.warning("default draft LLM unavailable: %s", exc)
        llm = None
    _default_llm_cache["resolved"] = True
    _default_llm_cache["llm"] = llm
    return llm


def default_service() -> CommentEngagementService:
    return CommentEngagementService(default_store(), default_llm())


def resolve_channel_id(channel_id: str) -> str:
    """Resolve an owner channel reference to its internal
    ``channels.id`` (internal id or YouTube channel id accepted).
    Raises ``ApiError`` 404 ``channel_not_found`` for unknown owners."""
    from app import db as db_module
    from app.comments.schema import ensure_comments_schema
    from app.errors import ApiError

    conn = db_module.get_connection()
    try:
        ensure_comments_schema(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
        where_deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
        row = conn.execute(
            "SELECT id FROM channels WHERE (id = ? OR youtube_channel_id = ?)"
            f" {where_deleted} LIMIT 1",
            (str(channel_id), str(channel_id)),
        ).fetchone()
        if row is None:
            raise ApiError(
                404, "channel_not_found",
                f"No channel with id '{channel_id}'.",
            )
        return str(row[0])
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _api_error_for(exc: Exception) -> Exception:
    """Map engagement/store errors onto the ApiError envelope."""
    from app.errors import ApiError

    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, CommentsScopeError):
        # Cross-channel existence must not leak: report as not found.
        return ApiError(404, "not_found", str(exc))
    if isinstance(exc, CommentsNotFoundError):
        return ApiError(404, "not_found", str(exc))
    if isinstance(exc, IllegalTransitionError):
        return ApiError(409, "illegal_transition", str(exc))
    if isinstance(exc, DraftGenerationError):
        return ApiError(502, "draft_generation_failed", str(exc))
    if isinstance(exc, ValueError):
        return ApiError(422, "invalid_request", str(exc))
    return exc


# --- API operations (thin: resolve owner, delegate, map errors) -----------

async def api_generate_draft(channel_id: str, comment_id: str,
                             service: Optional[CommentEngagementService] = None) -> dict:
    channel_pk = resolve_channel_id(channel_id)
    svc = service or default_service()
    try:
        return await svc.generate_draft(comment_id, channel_id=channel_pk)
    except Exception as exc:
        raise _api_error_for(exc) from exc


def api_list_drafts(channel_id: str, *, status: Optional[str] = None,
                    service: Optional[CommentEngagementService] = None) -> list[dict]:
    channel_pk = resolve_channel_id(channel_id)
    svc = service or default_service()
    try:
        return svc.list_drafts(channel_pk, status=status)
    except Exception as exc:
        raise _api_error_for(exc) from exc


def api_decide_draft(channel_id: str, draft_id: str, decision: str, *,
                     decided_by: Optional[str] = None,
                     service: Optional[CommentEngagementService] = None) -> dict:
    channel_pk = resolve_channel_id(channel_id)
    svc = service or default_service()
    try:
        return svc.decide_draft(
            draft_id, decision, channel_id=channel_pk, decided_by=decided_by
        )
    except Exception as exc:
        raise _api_error_for(exc) from exc


def api_edit_draft(channel_id: str, draft_id: str, draft_text: str, *,
                   edited_by: Optional[str] = None,
                   service: Optional[CommentEngagementService] = None) -> dict:
    """Edit a proposed draft's text (LOCAL only; never posted)."""
    channel_pk = resolve_channel_id(channel_id)
    svc = service or default_service()
    try:
        return svc.edit_draft(
            draft_id, draft_text, channel_id=channel_pk,
            edited_by=edited_by,
        )
    except Exception as exc:
        raise _api_error_for(exc) from exc


def api_list_moderation_queue(channel_id: str, *,
                              status: Optional[str] = "pending",
                              service: Optional[CommentEngagementService] = None) -> list[dict]:
    channel_pk = resolve_channel_id(channel_id)
    svc = service or default_service()
    try:
        return svc.list_moderation_queue(channel_pk, status=status)
    except Exception as exc:
        raise _api_error_for(exc) from exc


def api_decide_moderation_flag(channel_id: str, flag_id: str, decision: str, *,
                               decided_by: Optional[str] = None,
                               service: Optional[CommentEngagementService] = None) -> dict:
    channel_pk = resolve_channel_id(channel_id)
    svc = service or default_service()
    try:
        return svc.decide_moderation_flag(
            flag_id, decision, channel_id=channel_pk, decided_by=decided_by
        )
    except Exception as exc:
        raise _api_error_for(exc) from exc
