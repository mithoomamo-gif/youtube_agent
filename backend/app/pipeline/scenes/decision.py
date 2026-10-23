"""Stock-vs-AI sourcing decision — deterministic, pure, logged.

Roadmap §12 / Part 6.4 fixes a top-down order; this module implements it
exactly, with no network calls and no randomness, so the same inputs
always produce the same ``(choice, reason_code)``:

1. **Safety pre-check first** — a safety-restricted scene is routed to
   the safest programmatic fallback before any aesthetic choice.
2. **DATA / TEXTUAL -> programmatic, always** (chart / text card).
   Never stock, never generative.
3. **TRANSITION -> channel motion template** (programmatic graphic).
4. **HERO cap** — a 4th HERO in one video loses its hero upgrade.
5. **REAL_WORLD -> stock first**, but only a candidate that passes the
   relevance test (cosine >= 0.22), is licence-clean, carries no
   watermark/logo, and — for a *named* subject — is verified to depict
   that subject.  Rejected stock falls through to AI image.
6. **AI image** when stock fails / the subject is named / the scene is
   a specific event, character moment or abstract concept / HERO —
   subject to the AI budget (<= 40% of scenes) and the hard per-video
   generation-credit cap.  Over budget the chain degrades towards
   graphics, never towards repetition.
7. **AI video is a restricted tier**: HERO / signature scenes only,
   premium flag required, <= 10% of scenes.

Fallback chain (always defined): AI image -> broadened stock ->
stylised graphic / text card.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    AI_SCENE_BUDGET_FRACTION,
    AI_VIDEO_BUDGET_FRACTION,
    MAX_HERO_SCENES,
    STOCK_RELEVANCE_FLOOR,
    ReasonCode,
    SceneClass,
    SourceKind,
)


class StockCandidate(BaseModel):
    """One stock search result, pre-scored by the stock layer (Agent 15)."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    #: Image-text embedding cosine of asset vs the narration sentence.
    relevance_cosine: Optional[float] = Field(default=None, ge=-1, le=1)
    licence_ok: bool = True
    has_watermark: bool = False
    has_logo: bool = False
    #: For named subjects: verified to depict *that* subject (not a
    #: different place/person dressed as it).  None = unverified.
    subject_verified: Optional[bool] = None
    width_px: Optional[int] = None

    def passes_relevance(self, *, named_subject: bool) -> bool:
        """The automated stock relevance test (roadmap step 4)."""
        if not self.licence_ok or self.has_watermark or self.has_logo:
            return False
        if self.relevance_cosine is None or self.relevance_cosine < STOCK_RELEVANCE_FLOOR:
            return False
        if named_subject and self.subject_verified is not True:
            return False
        return True


class SceneFeatures(BaseModel):
    """Everything the decision needs to know about one scene."""

    model_config = ConfigDict(extra="forbid")

    scene_class: SceneClass
    #: The subject is a specific named place / person / event.
    is_named_subject: bool = False
    #: Safety pre-check restricted this scene before aesthetic choice.
    safety_restricted: bool = False
    #: Planner asks for the AI-video tier (HERO / channel signature).
    wants_ai_video: bool = False


class PlanBudgetState(BaseModel):
    """Per-video budget counters, snapshotted by the planner per scene."""

    model_config = ConfigDict(extra="forbid")

    total_scenes: int = Field(ge=1)
    ai_scenes_so_far: int = Field(default=0, ge=0)
    ai_video_scenes_so_far: int = Field(default=0, ge=0)
    hero_scenes_so_far: int = Field(default=0, ge=0)
    credits_used: int = Field(default=0, ge=0)
    #: Hard per-video generation-credit cap (cost controller).
    credit_cap: Optional[int] = Field(default=None, ge=0)
    #: Credit cost of one AI image / one AI video in this stack.
    ai_image_credit_cost: int = Field(default=1, ge=0)
    ai_video_credit_cost: int = Field(default=5, ge=0)
    premium_enabled: bool = False
    ai_fraction_cap: float = Field(default=AI_SCENE_BUDGET_FRACTION, gt=0, le=1)
    ai_video_fraction_cap: float = Field(
        default=AI_VIDEO_BUDGET_FRACTION, gt=0, le=1
    )

    def ai_image_allowed(self) -> tuple[bool, Optional[ReasonCode]]:
        """(allowed, blocking reason) for one more AI-sourced scene."""
        if (self.ai_scenes_so_far + 1) / self.total_scenes > self.ai_fraction_cap + 1e-9:
            return False, ReasonCode.AI_BUDGET_EXCEEDED
        if (
            self.credit_cap is not None
            and self.credits_used + self.ai_image_credit_cost > self.credit_cap
        ):
            return False, ReasonCode.CREDIT_CAP_EXCEEDED
        return True, None

    def ai_video_allowed(self) -> tuple[bool, Optional[ReasonCode]]:
        """(allowed, blocking reason) for one AI-video scene."""
        if not self.premium_enabled:
            return False, ReasonCode.AI_VIDEO_PREMIUM
        if (
            (self.ai_video_scenes_so_far + 1) / self.total_scenes
            > self.ai_video_fraction_cap + 1e-9
        ):
            return False, ReasonCode.AI_VIDEO_CAP_EXCEEDED
        allowed, reason = self.ai_image_allowed()
        if not allowed:
            return False, reason
        if (
            self.credit_cap is not None
            and self.credits_used + self.ai_video_credit_cost > self.credit_cap
        ):
            return False, ReasonCode.CREDIT_CAP_EXCEEDED
        return True, None


class SourceDecision(NamedTuple):
    """``(choice, reason_code)`` plus a human-readable detail string.

    A NamedTuple so callers may unpack it as the roadmap's pair —
    ``choice, reason = decide_source(...)[:2]`` — or use the fields.
    """

    choice: SourceKind
    reason_code: ReasonCode
    detail: str = ""


def _best_candidate(
    candidates: Sequence[StockCandidate],
) -> Optional[StockCandidate]:
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda c: c.relevance_cosine if c.relevance_cosine is not None else -1.0,
    )


def _ai_image_or_fallback(
    budget: PlanBudgetState,
    *,
    preferred_reason: ReasonCode,
    detail: str,
) -> SourceDecision:
    """AI image if the budget allows it, else degrade to a graphic."""
    allowed, blocking = budget.ai_image_allowed()
    if allowed:
        return SourceDecision(SourceKind.AI_IMAGE, preferred_reason, detail)
    return SourceDecision(
        SourceKind.GRAPHIC,
        blocking or ReasonCode.FALLBACK_GRAPHIC,
        f"{detail} AI budget blocks generation ({blocking}); "
        f"degraded to a programmatic graphic.",
    )


def decide_source(
    features: SceneFeatures,
    candidates: Sequence[StockCandidate],
    budget: PlanBudgetState,
) -> SourceDecision:
    """Decide one scene's source, deterministically, in roadmap order.

    Pure function: no I/O, no network, no randomness.
    """
    scene_class = features.scene_class
    best = _best_candidate(candidates)

    # 1 — Safety pre-check first -------------------------------------------
    if features.safety_restricted:
        return SourceDecision(
            SourceKind.TEXT_CARD,
            ReasonCode.SAFETY_ROUTE,
            "Safety pre-check restricted this scene before any aesthetic "
            "choice; routed to the safest programmatic fallback.",
        )

    # 2 — DATA / TEXTUAL -> programmatic, always ----------------------------
    if scene_class is SceneClass.DATA:
        return SourceDecision(
            SourceKind.GRAPHIC,
            ReasonCode.PROGRAMMATIC_DATA,
            "DATA scenes are rendered as deterministic programmatic "
            "graphics (charts/timelines/maps) — never stock, never generative.",
        )
    if scene_class is SceneClass.TEXTUAL:
        return SourceDecision(
            SourceKind.TEXT_CARD,
            ReasonCode.PROGRAMMATIC_TEXT,
            "TEXTUAL scenes (quotes/verses) are programmatic text cards — "
            "generated text is unreliable and stock cannot carry scripture.",
        )

    # 3 — TRANSITION -> channel motion template -----------------------------
    if scene_class is SceneClass.TRANSITION:
        return SourceDecision(
            SourceKind.GRAPHIC,
            ReasonCode.TRANSITION_TEMPLATE,
            "TRANSITION scenes use the channel motion template.",
        )

    # 4 — HERO: cap first, then the upgrade tier ----------------------------
    if scene_class is SceneClass.HERO:
        if budget.hero_scenes_so_far >= MAX_HERO_SCENES:
            # The 4th "hero" is not a hero: degrade to the generic chain.
            if best is not None and best.passes_relevance(
                named_subject=features.is_named_subject
            ):
                return SourceDecision(
                    SourceKind.STOCK,
                    ReasonCode.HERO_CAP_EXCEEDED,
                    f"HERO cap ({MAX_HERO_SCENES}/video) reached; scene "
                    f"downgraded and sourced from passing stock.",
                )
            return SourceDecision(
                SourceKind.GRAPHIC,
                ReasonCode.HERO_CAP_EXCEEDED,
                f"HERO cap ({MAX_HERO_SCENES}/video) reached; scene "
                f"downgraded to a programmatic graphic.",
            )
        if features.wants_ai_video:
            allowed, _blocking = budget.ai_video_allowed()
            if allowed:
                return SourceDecision(
                    SourceKind.AI_VIDEO,
                    ReasonCode.HERO_UPGRADE,
                    "HERO scene upgraded to AI video (premium tier, "
                    "within the 10% AI-video cap).",
                )
        return _ai_image_or_fallback(
            budget,
            preferred_reason=ReasonCode.HERO_UPGRADE,
            detail="HERO scene upgraded to a generated image.",
        )

    # 5/6 — Class-driven AI routes ------------------------------------------
    if scene_class is SceneClass.ABSTRACT_CONCEPT:
        return _ai_image_or_fallback(
            budget,
            preferred_reason=ReasonCode.ABSTRACT_AI,
            detail="ABSTRACT_CONCEPT: the metaphor must be constructed; "
            "random mood stock is never acceptable.",
        )
    if scene_class is SceneClass.SPECIFIC_EVENT:
        return _ai_image_or_fallback(
            budget,
            preferred_reason=(
                ReasonCode.NAMED_SUBJECT_AI
                if features.is_named_subject
                else ReasonCode.SPECIFIC_EVENT_AI
            ),
            detail="SPECIFIC_EVENT: generic stock would mislead (a stock "
            "'ancient city' for a named ancient city is a factual error); "
            "illustration is generated instead.",
        )
    if scene_class is SceneClass.CHARACTER_MOMENT:
        return _ai_image_or_fallback(
            budget,
            preferred_reason=ReasonCode.CHARACTER_AI,
            detail="CHARACTER_MOMENT: character-locked generation; a "
            "recognisable real-person likeness is never sourced.",
        )

    # REAL_WORLD — stock first ----------------------------------------------
    assert scene_class is SceneClass.REAL_WORLD  # only class left
    if best is not None and best.passes_relevance(
        named_subject=features.is_named_subject
    ):
        return SourceDecision(
            SourceKind.STOCK,
            ReasonCode.STOCK_MATCH,
            f"Stock candidate {best.candidate_id} passed the relevance "
            f"test (cosine {best.relevance_cosine} >= {STOCK_RELEVANCE_FLOOR}).",
        )

    # Stock failed: name *why*, then fall through to AI image.
    if not candidates:
        fail_reason, fail_detail = (
            ReasonCode.NO_STOCK_EXISTS,
            "No stock candidates exist for this scene.",
        )
    elif (
        features.is_named_subject
        and best is not None
        and best.relevance_cosine is not None
        and best.relevance_cosine >= STOCK_RELEVANCE_FLOOR
        and best.subject_verified is not True
    ):
        fail_reason, fail_detail = (
            ReasonCode.STOCK_UNVERIFIED_NAMED,
            "Best stock candidate is not verified to depict the named "
            "subject; stock of a different place presented as the named "
            "one is a factual error.",
        )
    else:
        fail_reason, fail_detail = (
            ReasonCode.STOCK_BELOW_THRESHOLD,
            f"Best stock candidate failed the relevance test "
            f"(cosine {best.relevance_cosine if best else None} vs floor "
            f"{STOCK_RELEVANCE_FLOOR}, or licence/watermark/quality gate).",
        )

    decision = _ai_image_or_fallback(
        budget,
        preferred_reason=fail_reason,
        detail=fail_detail,
    )
    # When AI is allowed the stock-failure reason *is* the logged reason;
    # when it is not, the budget reason wins (it explains the graphic).
    return decision


def validate_ai_budget(scenes: Sequence["object"]) -> list[str]:
    """Plan-level budget check: AI <= 40% of scenes, AI video <= 10%.

    Accepts any objects with a ``chosen_source`` attribute (SceneRecord).
    Returns human-readable violation strings (empty = within budget).
    """
    from .models import SceneRecord  # local import: typing convenience only

    records = [s for s in scenes if isinstance(s, SceneRecord)]
    if not records:
        return []
    total = len(records)
    ai = sum(1 for s in records if s.chosen_source in (SourceKind.AI_IMAGE, SourceKind.AI_VIDEO))
    ai_video = sum(1 for s in records if s.chosen_source is SourceKind.AI_VIDEO)
    problems: list[str] = []
    if ai / total > AI_SCENE_BUDGET_FRACTION + 1e-9:
        problems.append(
            f"AI budget exceeded: {ai}/{total} scenes are AI-sourced "
            f"({ai / total:.0%}) > {AI_SCENE_BUDGET_FRACTION:.0%} cap"
        )
    if ai_video / total > AI_VIDEO_BUDGET_FRACTION + 1e-9:
        problems.append(
            f"AI-video budget exceeded: {ai_video}/{total} scenes "
            f"({ai_video / total:.0%}) > {AI_VIDEO_BUDGET_FRACTION:.0%} cap"
        )
    heroes = sum(1 for s in records if s.scene_class is SceneClass.HERO)
    if heroes > MAX_HERO_SCENES:
        problems.append(
            f"HERO cap exceeded: {heroes} HERO scenes > {MAX_HERO_SCENES} per video"
        )
    return problems
