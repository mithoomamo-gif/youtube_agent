"""Learning endpoints (Round 5 — consumption seam + production lessons).

All routes are nested under the OWNER channel, mirroring the Style
Lab router's convention::

    GET  /api/v1/channels/{channel_id}/learning/rules
    POST /api/v1/channels/{channel_id}/learning/topic-suggestions
    GET  /api/v1/channels/{channel_id}/learning/production-lessons
    GET  /api/v1/channels/{channel_id}/learning/rules/history
    GET  /api/v1/channels/{channel_id}/learning/calibration
    GET  /api/v1/channels/{channel_id}/learning/publish-window
    POST /api/v1/channels/{channel_id}/learning/evaluate

The surfaces are deliberately SEPARATE:

* ``learning/rules`` — the performance (audience) rules for exactly
  ``(channel_id, niche)``: the active rules read through the
  consumption seam PLUS the scope's candidate rules, each item
  carrying its ``status`` (and evidence_count / confidence /
  version / thin-evidence label).  ``niche`` (query param) is
  required: there is no unscoped or cross-niche read.  The
  consumption seam itself (topic suggestions) stays active-only —
  candidates are listed here for visibility, never consumed as
  established rules.
* ``learning/topic-suggestions`` — the research consumption seam
  (:mod:`app.learning.consumption`): ranked topic suggestions for
  caller-supplied candidates, each carrying its ``why`` provenance
  (supporting rule ids + versions + evidence counts + confidence,
  or an explicit ``baseline_no_evidence`` label).  Rules travel as
  DATA in the response; nothing here touches a prompt template.
* ``learning/production-lessons`` — the production-lessons view
  (:mod:`app.learning.lessons`): gate/approval reject reasons
  aggregated per (channel, niche).  A different structure from
  rules (``lessons`` / ``observations`` split by the evidence
  threshold), a different source (``approvals``), and no rule
  fields anywhere in it.

Channel resolution accepts the internal ``channels.id`` or the
YouTube channel id (the channels router's convention) and answers
404 ``channel_not_found`` for an unknown owner, so a scope that
does not exist never silently reads as an empty scope.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.errors import ApiError

router = APIRouter(prefix="/api/v1/channels", tags=["learning"])


class TopicSuggestionsBody(BaseModel):
    niche: str
    candidates: list[str] = []


def _conn_factory():
    from app import db as db_module

    return db_module.get_connection


def _resolve_channel_id(channel_id: str) -> str:
    """Internal channel id for ``channel_id`` (id or YouTube id)."""
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
        where_deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
        row = conn.execute(
            "SELECT id FROM channels WHERE (id = ? OR youtube_channel_id = ?)"
            f" {where_deleted} LIMIT 1",
            (channel_id, channel_id),
        ).fetchone()
        if row is None:
            raise ApiError(
                404,
                "channel_not_found",
                f"Channel '{channel_id}' was not found.",
            )
        return str(row[0])
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _require_niche(niche: Optional[str]) -> str:
    value = str(niche or "").strip()
    if not value:
        raise ApiError(
            422,
            "niche_required",
            "A niche scope is required: learning is never pooled "
            "across niches.",
            details={"field": "niche"},
        )
    return value


@router.get("/{channel_id}/learning/rules")
async def list_learning_rules(
    channel_id: str, niche: Optional[str] = Query(default=None)
) -> dict:
    """Active + candidate rules for exactly (channel_id, niche).

    Active rules come through the consumption seam (normalised);
    candidate rules are appended from the Learning Store, normalised
    the same way.  Every item carries ``status`` so the two are
    never confused, and thin-evidence rules are labelled via
    ``is_thin_evidence`` — listed, never hidden.  Superseded rules
    are history (see ``learning/rules/history``), not current rules.
    """
    from app.learning.consumption import fetch_active_rules, normalise_rule
    from app.learning.store import LearningStore

    internal_id = _resolve_channel_id(channel_id)
    scope_niche = _require_niche(niche)
    factory = _conn_factory()
    items: list[dict] = []
    for rule in fetch_active_rules(
        internal_id, scope_niche, conn_factory=factory
    ):
        items.append({**rule, "status": "active"})
    store = LearningStore(factory)
    for rule in store.list_candidate_rules(internal_id, scope_niche):
        items.append({**normalise_rule(rule), "status": "candidate"})
    return {
        "channel_id": internal_id,
        "niche": scope_niche,
        "items": items,
        "count": len(items),
        "next_cursor": None,
    }


@router.post("/{channel_id}/learning/topic-suggestions")
async def topic_suggestions(
    channel_id: str, body: TopicSuggestionsBody, request: Request
) -> dict:
    """Ranked topic suggestions with per-suggestion rule provenance."""
    from app.learning.consumption import build_topic_suggestions

    internal_id = _resolve_channel_id(channel_id)
    scope_niche = _require_niche(body.niche)
    result = build_topic_suggestions(
        internal_id,
        scope_niche,
        body.candidates,
        conn_factory=_conn_factory(),
    )
    result["channel_id"] = internal_id
    return result


@router.get("/{channel_id}/learning/production-lessons")
async def production_lessons(
    channel_id: str, niche: Optional[str] = Query(default=None)
) -> dict:
    """Production lessons (reject-reason aggregates) for the scope.

    Separate view from performance rules: the response carries
    ``lessons`` / ``observations`` entries built from approval
    decisions — never learning-rule fields.
    """
    from app.learning.lessons import production_lessons_for_scope

    internal_id = _resolve_channel_id(channel_id)
    scope_niche = _require_niche(niche)
    view = production_lessons_for_scope(
        _conn_factory(), channel_id=internal_id, niche=scope_niche
    )
    view.pop("entries", None)
    return view


# ---------------------------------------------------------------------------
# Learning Loop API surface (app.learning.service): rule history,
# calibration summary, publish-window suggestion, batch trigger.
#
# These delegate to app.learning.service, which owns the sibling
# seams (calibration read model, update engine bridged to the
# Learning Store, publish-window eligibility) and the channel
# resolution / niche enforcement for its routes.  Errors surface
# through the same ApiError envelope as the routes above.
# ---------------------------------------------------------------------------


class LearningEvaluateBody(BaseModel):
    """Body for the manual batch-evaluation trigger.  ``niche`` may
    also be supplied as the ``?niche=`` query parameter; one of the
    two is required (learning is never unscoped)."""

    niche: Optional[str] = None
    snapshot_point: Optional[str] = None  # 'D+7' | 'D+28' | None (both)


@router.get("/{channel_id}/learning/rules/history")
async def list_learning_rule_history(
    channel_id: str,
    niche: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
) -> dict:
    """Rule history for (channel, niche, kind) — superseded versions
    included, newest first.  ``niche`` and ``kind`` are required."""
    from app.learning import service

    scope_niche = _require_niche(niche)
    if not str(kind or "").strip():
        raise ApiError(
            422,
            "validation_error",
            "A rule kind is required for history.",
            details={"field": "kind"},
        )
    return service.get_rule_history(channel_id, scope_niche, kind)


@router.get("/{channel_id}/learning/calibration")
async def get_learning_calibration(
    channel_id: str, niche: Optional[str] = Query(default=None)
) -> dict:
    """Calibration summary for (channel, niche): per metric, the
    D+7 and D+28 bias / MAPE / band-coverage from the calibration
    sibling's read model, with insufficient-evidence labelling."""
    from app.learning import service

    scope_niche = _require_niche(niche)
    return service.get_calibration_summary(channel_id, scope_niche)


@router.get("/{channel_id}/learning/publish-window")
async def get_learning_publish_window(channel_id: str) -> dict:
    """Publish-window suggestion for the channel: the eligible
    result, or the honest not-enough-data structure — returned
    verbatim, never flattened into a fake suggestion."""
    from app.learning import service

    return service.get_publish_window_suggestion(channel_id)


@router.post("/{channel_id}/learning/evaluate")
async def trigger_learning_evaluate(
    channel_id: str,
    body: Optional[LearningEvaluateBody] = None,
    niche: Optional[str] = Query(default=None),
    snapshot_point: Optional[str] = Query(default=None),
) -> dict:
    """Run the learning update engine ONCE for (channel, niche).

    An explicit, user-invoked trigger — not a scheduler: no cron and
    no background loop is created.  Evaluates D+7 and D+28 (or the
    single requested snapshot point) and returns the batch reports.
    """
    from app.learning import service

    niche_value = niche
    point_value = snapshot_point
    if body is not None:
        if niche_value is None:
            niche_value = body.niche
        if point_value is None:
            point_value = body.snapshot_point
    scope_niche = _require_niche(niche_value)
    return service.trigger_learning_batch(
        channel_id, scope_niche, snapshot_point=point_value
    )
