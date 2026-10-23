"""Comment Engagement endpoints (Round 5 — Agent 28, ENGAGEMENT half).

All routes are nested under the OWNER channel::

    POST /api/v1/channels/{channel_id}/comments/{comment_id}/drafts   (201)
    GET  /api/v1/channels/{channel_id}/comment-drafts
    PATCH /api/v1/channels/{channel_id}/comment-drafts/{draft_id}
    POST /api/v1/channels/{channel_id}/comment-drafts/{draft_id}/approve
    POST /api/v1/channels/{channel_id}/comment-drafts/{draft_id}/reject
    POST /api/v1/channels/{channel_id}/comment-drafts/{draft_id}/discard
    GET  /api/v1/channels/{channel_id}/moderation-queue
    POST /api/v1/channels/{channel_id}/moderation-queue/{flag_id}/decision

Conventions follow the sibling routers: the ``ApiError`` envelope
from :mod:`app.errors`, list responses as
``{"items": [...], "next_cursor": None}``, creates as 201, and the
launch-token middleware (mounted by ``app.main``) guards everything.

=====================================================================
POSTING IS NOT WIRED — APPROVAL NEVER POSTS
=====================================================================
Approving a draft here only marks the LOCAL draft row ``approved``.
There is NO posting implementation in this round: no endpoint in
this router (or anywhere in the comments feature) sends anything
to YouTube.  Every draft response carries ``posted: false`` and
``posting: "not_wired"`` so no consumer can mistake an approval
for a published reply.  A future round may wire posting for
connected channels behind this exact approval gate.

Moderation decisions are likewise LOCAL records only: the queue's
suggested actions are labels for a human reviewer, and recording a
decision (dismiss / mark-reviewed) executes nothing against
YouTube — every flag response carries ``action_executed: false``.

Isolation posture: every endpoint is owner-scoped through
:mod:`app.comments.engagement` — a comment / draft / flag under
another channel answers 404 (never 403), exactly like the rest of
the codebase.

Sync / listing / analysis surface (added by the Comments REST API
worker, delegating to :mod:`app.comments.api_service`)::

    POST /api/v1/channels/{channel_id}/videos/{video_id}/comments/sync
    GET  /api/v1/channels/{channel_id}/videos/{video_id}/comments/sync-status
    GET  /api/v1/channels/{channel_id}/videos/{video_id}/comments
    GET  /api/v1/channels/{channel_id}/videos/{video_id}/analysis
    POST /api/v1/channels/{channel_id}/videos/{video_id}/analyse
    GET  /api/v1/channels/{channel_id}/analysis
    POST /api/v1/channels/{channel_id}/analyse
    GET  /api/v1/channels/{channel_id}/drafts
    GET  /api/v1/channels/{channel_id}/drafts/{draft_id}
    PATCH /api/v1/channels/{channel_id}/drafts/{draft_id}
    POST /api/v1/channels/{channel_id}/drafts/{draft_id}/approve
    POST /api/v1/channels/{channel_id}/drafts/{draft_id}/reject
    POST /api/v1/channels/{channel_id}/drafts/{draft_id}/discard
    GET  /api/v1/channels/{channel_id}/moderation
    POST /api/v1/channels/{channel_id}/moderation/{flag_id}/decision

``{video_id}`` is the YouTube video id.  Sync always answers 200
with the sync worker's typed result — ``status='not_connected'``
when no usable YouTube credential exists (zero HTTP attempted),
matching the ``metadata_unavailable`` precedent above.  Analysis
reads answer ``status='not_analysed'`` when no rollup has been
persisted; numbers are never fabricated.  The ``/drafts`` and
``/moderation`` paths are the canonical spellings of the
``/comment-drafts`` / ``/moderation-queue`` endpoints above (same
service, same local-only semantics; the moderation decision
vocabulary here is ``dismiss`` | ``reviewed``).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.comments import api_service as service
from app.comments import engagement

router = APIRouter(prefix="/api/v1/channels", tags=["comments"])


class ModerationDecision(BaseModel):
    """A LOCAL human decision on a queued moderation flag.

    ``decision``: ``'dismiss'`` | ``'mark_reviewed'``.
    ``decided_by``: optional free-text actor label (this round has
    no authenticated-actor concept; the timestamp is always kept).
    """

    decision: str
    decided_by: Optional[str] = None


class DraftDecision(BaseModel):
    """Optional actor label for a draft approve/reject/discard decision."""

    decided_by: Optional[str] = None


class DraftEdit(BaseModel):
    """Edit a ``proposed`` draft's text (R5-004).

    ``draft_text``: the replacement reply text (non-empty).
    ``edited_by``: optional free-text actor label, recorded in the
    draft's provenance alongside the edit timestamp and the
    previous/new text.  The edit is a LOCAL update only — nothing
    is posted, ever.
    """

    draft_text: str
    edited_by: Optional[str] = None


# ---------------------------------------------------------------------------
# Draft replies — generate + approve/reject (LOCAL ONLY, never posted)
# ---------------------------------------------------------------------------


@router.post("/{channel_id}/comments/{comment_id}/drafts", status_code=201)
async def generate_draft(channel_id: str, comment_id: str) -> dict:
    """Generate a suggested reply draft for a stored comment.

    The draft is created in ``proposed`` status with its generation
    provenance recorded.  Generating a draft posts nothing.
    """
    return await engagement.api_generate_draft(channel_id, comment_id)


@router.get("/{channel_id}/comment-drafts")
async def list_drafts(channel_id: str, status: Optional[str] = None) -> dict:
    items = engagement.api_list_drafts(channel_id, status=status)
    return {"items": items, "next_cursor": None}


@router.post("/{channel_id}/comment-drafts/{draft_id}/approve")
async def approve_draft(channel_id: str, draft_id: str,
                        body: Optional[DraftDecision] = None) -> dict:
    """Mark a proposed draft APPROVED — a local decision only.

    THIS DOES NOT POST THE REPLY.  Posting is not wired in this
    round; the response carries ``posted: false`` /
    ``posting: "not_wired"``.
    """
    return engagement.api_decide_draft(
        channel_id, draft_id, "approved",
        decided_by=(body.decided_by if body else None),
    )


@router.post("/{channel_id}/comment-drafts/{draft_id}/reject")
async def reject_draft(channel_id: str, draft_id: str,
                       body: Optional[DraftDecision] = None) -> dict:
    """Mark a proposed draft REJECTED (terminal, local only)."""
    return engagement.api_decide_draft(
        channel_id, draft_id, "rejected",
        decided_by=(body.decided_by if body else None),
    )


@router.patch("/{channel_id}/comment-drafts/{draft_id}")
async def edit_draft(channel_id: str, draft_id: str,
                     body: DraftEdit) -> dict:
    """Edit a PROPOSED draft's text — a local update only.

    The edit is recorded in the draft's provenance (who / when /
    previous + new text).  Editing a terminal draft answers 409.
    THIS DOES NOT POST ANYTHING."""
    return engagement.api_edit_draft(
        channel_id, draft_id, body.draft_text, edited_by=body.edited_by,
    )


@router.post("/{channel_id}/comment-drafts/{draft_id}/discard")
async def discard_draft(channel_id: str, draft_id: str,
                        body: Optional[DraftDecision] = None) -> dict:
    """Mark a proposed draft DISCARDED — a terminal state DISTINCT
    from ``rejected`` (local only; nothing is posted)."""
    return engagement.api_decide_draft(
        channel_id, draft_id, "discarded",
        decided_by=(body.decided_by if body else None),
    )


# ---------------------------------------------------------------------------
# Moderation queue — labels + LOCAL human decisions only
# ---------------------------------------------------------------------------


@router.get("/{channel_id}/moderation-queue")
async def list_moderation_queue(channel_id: str,
                                status: Optional[str] = "pending") -> dict:
    items = engagement.api_list_moderation_queue(channel_id, status=status)
    return {"items": items, "next_cursor": None}


@router.post("/{channel_id}/moderation-queue/{flag_id}/decision")
async def decide_moderation_flag(channel_id: str, flag_id: str,
                                 body: ModerationDecision) -> dict:
    """Record a LOCAL human decision (dismiss / mark_reviewed).

    Nothing is executed against YouTube — the response carries
    ``action_executed: false``.
    """
    return engagement.api_decide_moderation_flag(
        channel_id, flag_id, body.decision, decided_by=body.decided_by
    )


# ---------------------------------------------------------------------------
# Sync — trigger + status (delegates to the fetch/sync worker's service)
# ---------------------------------------------------------------------------


@router.post("/{channel_id}/videos/{video_id}/comments/sync")
async def sync_comments(channel_id: str, video_id: str,
                        request: Request) -> dict:
    """Trigger a comment sync for one video (read-only fetch +
    idempotent upsert).  Always 200 with the typed result —
    ``status='not_connected'`` included (see module docstring)."""
    return await service.sync_video(channel_id, video_id, request)


@router.get("/{channel_id}/videos/{video_id}/comments/sync-status")
async def sync_status(channel_id: str, video_id: str) -> dict:
    """Persisted sync state for the video (``never_synced`` when no
    sync has ever run)."""
    return service.sync_status(channel_id, video_id)


class ChannelSyncRequest(BaseModel):
    """Channel-level sync trigger: one video when ``video_id`` is
    given, every known video of the channel otherwise."""

    video_id: Optional[str] = None


class ChannelAnalysisRequest(BaseModel):
    """Channel-level analysis trigger: one video when ``video_id``
    is given, the whole channel otherwise."""

    video_id: Optional[str] = None


@router.get("/{channel_id}/comments/videos")
async def list_comment_videos(channel_id: str) -> dict:
    """Per-video sync rows for the channel (sync status, stored
    comment count, last sync, analysis status per known video)."""
    return service.list_comment_videos(channel_id)


@router.post("/{channel_id}/comments/sync")
async def sync_channel_comments(channel_id: str, request: Request,
                                body: Optional[ChannelSyncRequest] = None) -> dict:
    """Sync comments for one video (``{video_id}``) or all of the
    channel's known videos.  Always 200 with typed per-video results
    — ``not_connected`` / ``no_videos`` included."""
    return await service.sync_channel(
        channel_id, video_id=body.video_id if body else None,
        request=request,
    )


@router.post("/{channel_id}/comments/analysis")
async def analyse_channel_comments(
        channel_id: str,
        body: Optional[ChannelAnalysisRequest] = None) -> dict:
    """Run comment analysis for one video (``{video_id}``) or the
    whole channel; the persisted rollup is returned."""
    return await service.analyse_channel_or_video(
        channel_id, video_id=body.video_id if body else None
    )


@router.get("/{channel_id}/comments/rollups")
async def comment_rollups(channel_id: str) -> dict:
    """Channel rollup + latest rollup per analysed video (persisted
    rows only; ``status='not_analysed'`` when none exist)."""
    return service.rollups(channel_id)


# ---------------------------------------------------------------------------
# Stored comments — paginated, flags attached
# ---------------------------------------------------------------------------


@router.get("/{channel_id}/videos/{video_id}/comments")
async def list_comments(channel_id: str, video_id: str,
                        limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0),
                        top_level_only: bool = False) -> dict:
    """Paginated stored comments for the video, newest first, each
    with its moderation flags attached (``flags`` / ``flag_count``)."""
    return service.list_video_comments(
        channel_id, video_id, limit=limit, offset=offset,
        top_level_only=top_level_only,
    )


# ---------------------------------------------------------------------------
# Analysis rollups — persisted reads + engine triggers
# ---------------------------------------------------------------------------


@router.get("/{channel_id}/videos/{video_id}/analysis")
async def video_analysis(channel_id: str, video_id: str) -> dict:
    """Latest persisted per-video rollup, or the honest
    ``not_analysed`` state (``analysis: null``)."""
    return service.video_analysis(channel_id, video_id)


@router.post("/{channel_id}/videos/{video_id}/analyse")
async def analyse_video(channel_id: str, video_id: str) -> dict:
    """Run the analysis engine over the video's STORED comments and
    persist the rollup (+ spam flags into the moderation queue).
    Zero stored comments -> ``status='no_comments'``, nothing
    persisted."""
    return await service.analyse_video(channel_id, video_id)


@router.get("/{channel_id}/analysis")
async def channel_analysis(channel_id: str) -> dict:
    """Latest persisted per-channel rollup, or ``not_analysed``."""
    return service.channel_analysis(channel_id)


@router.post("/{channel_id}/analyse")
async def analyse_channel(channel_id: str) -> dict:
    """Run the channel rollup over ALL stored comments of the channel."""
    return await service.analyse_channel(channel_id)


# ---------------------------------------------------------------------------
# Drafts — canonical /drafts spellings (same engagement service and
# local-only semantics as /comment-drafts above)
# ---------------------------------------------------------------------------


@router.get("/{channel_id}/drafts")
async def list_drafts_canonical(channel_id: str,
                                status: Optional[str] = None,
                                video_id: Optional[str] = None,
                                comment_id: Optional[str] = None) -> dict:
    """Drafts owned by the channel, filterable by ``status``
    (proposed / approved / rejected / discarded), ``video_id`` or
    ``comment_id``.
    Every draft carries ``posted: false`` / ``posting: 'not_wired'``."""
    return service.list_drafts(
        channel_id, status=status, video_id=video_id,
        comment_id=comment_id,
    )


@router.get("/{channel_id}/drafts/{draft_id}")
async def get_draft_canonical(channel_id: str, draft_id: str) -> dict:
    return service.get_draft(channel_id, draft_id)


@router.post("/{channel_id}/drafts/{draft_id}/approve")
async def approve_draft_canonical(channel_id: str, draft_id: str,
                                  body: Optional[DraftDecision] = None) -> dict:
    """Approve a proposed draft — LOCAL only; does NOT post it."""
    return service.decide_draft(
        channel_id, draft_id, "approved",
        decided_by=(body.decided_by if body else None),
    )


@router.post("/{channel_id}/drafts/{draft_id}/reject")
async def reject_draft_canonical(channel_id: str, draft_id: str,
                                 body: Optional[DraftDecision] = None) -> dict:
    """Reject a proposed draft (terminal, local only)."""
    return service.decide_draft(
        channel_id, draft_id, "rejected",
        decided_by=(body.decided_by if body else None),
    )


@router.patch("/{channel_id}/drafts/{draft_id}")
async def edit_draft_canonical(channel_id: str, draft_id: str,
                               body: DraftEdit) -> dict:
    """Edit a PROPOSED draft's text (local only; provenance recorded)."""
    return service.edit_draft(
        channel_id, draft_id, body.draft_text, edited_by=body.edited_by,
    )


@router.post("/{channel_id}/drafts/{draft_id}/discard")
async def discard_draft_canonical(channel_id: str, draft_id: str,
                                  body: Optional[DraftDecision] = None) -> dict:
    """Discard a proposed draft (terminal, DISTINCT from rejected,
    local only)."""
    return service.decide_draft(
        channel_id, draft_id, "discarded",
        decided_by=(body.decided_by if body else None),
    )


# ---------------------------------------------------------------------------
# Moderation — canonical /moderation spellings (decisions recorded
# locally only; suggested actions are labels, never executed)
# ---------------------------------------------------------------------------


@router.get("/{channel_id}/moderation")
async def list_moderation_canonical(channel_id: str,
                                    status: Optional[str] = "pending",
                                    flag_kind: Optional[str] = None,
                                    video_id: Optional[str] = None) -> dict:
    """Flagged comments + suggested-action labels for human review
    (default: the ``pending`` queue)."""
    return service.list_moderation(
        channel_id, status=status, flag_kind=flag_kind,
        video_id=video_id,
    )


@router.post("/{channel_id}/moderation/{flag_id}/decision")
async def decide_moderation_canonical(channel_id: str, flag_id: str,
                                      body: ModerationDecision) -> dict:
    """Record a LOCAL decision: ``dismiss`` | ``reviewed``
    (``mark_reviewed`` also accepted).  Nothing is executed against
    YouTube — the response carries ``action_executed: false``."""
    return service.decide_moderation(
        channel_id, flag_id, body.decision, decided_by=body.decided_by
    )
