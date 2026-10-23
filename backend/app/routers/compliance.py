"""Compliance Gate endpoints (Round 6 — roadmap's #1 launch blocker).

Routes::

    GET  /api/v1/settings/compliance               -> full state
    POST /api/v1/settings/compliance/transitions   -> deliberate,
         logged checklist transition; returns the full new state

There is deliberately NO PUT/PATCH/free-form edit endpoint: the
state can only move through the transition action, which requires
an ``actor`` and — for a ``passed`` transition — ``evidence``, and
appends an audit/history row per change (see
:mod:`app.compliance.service`).  A self-serve "mark everything
passed" flip is therefore impossible through this surface: the
overall state is derived from the four items, and each item needs
its own evidenced transition.

While the derived overall state is not ``passed``, Gate C forces
uploads to private and refuses public publishes (typed reason
``compliance_not_passed``) — the GET response states that effect
plainly in its ``effect`` field for the Settings UI.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.compliance import service
from app.errors import ApiError

router = APIRouter(prefix="/api/v1/settings/compliance", tags=["compliance"])


class TransitionBody(BaseModel):
    item_key: str
    to_status: str
    actor: str
    evidence: Optional[str] = None
    note: Optional[str] = None


@router.get("")
async def get_compliance() -> dict:
    """Checklist + derived overall + effect + recent history."""
    return service.get_state_from_default_db()


@router.post("/transitions")
async def post_transition(body: TransitionBody) -> dict:
    """One deliberate, logged checklist transition.

    Validation failures are typed: 404 ``compliance_item_not_found``,
    422 ``compliance_status_invalid`` / ``compliance_actor_required``
    / ``compliance_transition_noop`` /
    ``compliance_evidence_required``.
    """
    try:
        return service.transition_on_default_db(
            item_key=body.item_key,
            to_status=body.to_status,
            actor=body.actor,
            evidence=body.evidence,
            note=body.note,
        )
    except service.ComplianceTransitionError as exc:
        raise ApiError(
            exc.status_code, exc.code, exc.message, details={"item_key": body.item_key}
        ) from exc
