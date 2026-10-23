"""Comments Intelligence — router-facing service layer (Round 5, Agent 28).

This module is the ONLY place ``app.routers.comments`` talks to; it
composes the sibling Comments workers' modules and owns NO logic of
its own beyond owner resolution, error mapping and view shaping:

* :mod:`app.comments.store` — :class:`CommentsStore`, the
  owner-scoped store (comments / sync state / analyses / drafts /
  moderation flags).  All persistence goes through it.
* :mod:`app.comments.service` — ``sync_video_comments``: the fetch /
  sync worker's entry point (read-only ``commentThreads.list`` via
  :mod:`app.providers.youtube.comments`, idempotent upsert, honest
  typed states recorded in ``comment_sync_state``).
* :mod:`app.comment_intel` — :class:`CommentAnalysisEngine`
  (``analyse_stored_video`` / ``analyse_stored_channel``): analysis
  runs over STORED comments and persists its rollup + spam flags back
  through the store.  Rollup READS here are pure store reads
  (``latest_analysis``) — when no analysis row exists the honest
  answer is ``status='not_analysed'`` with ``analysis=None``; numbers
  are never fabricated.
* :mod:`app.comments.engagement` — :class:`CommentEngagementService`:
  draft generation (LLM T1 with a LABELLED deterministic fallback),
  the draft approval state machine and the moderation queue.  Its
  ``resolve_channel_id`` / ``default_store`` / ``default_service`` /
  ``default_llm`` factories are reused verbatim.

Owner scoping / isolation posture
---------------------------------
Every operation first resolves the owner channel (``channels.id``; a
YouTube channel id is also accepted, mirroring the sibling routers).
Unknown owner -> 404 ``channel_not_found``.  A comment / draft / flag
that exists under a DIFFERENT channel is reported as 404 with the
resource's ``*_not_found`` code, never 403 — existence must not leak
across channels (the store enforces the same rule: scoped reads
return ``None``, scoped writes raise ``CommentsScopeError``, which
this layer maps to the same 404).

Honest states (never fabricated, never raised raw)
--------------------------------------------------
* Sync: the sync service never raises for credential / transport /
  API failures — it returns a typed ``CommentSyncResult``.  The
  endpoint answers 200 with that payload in EVERY case, including
  ``status='not_connected'`` (no usable YouTube credential — zero
  HTTP attempted) and ``status='error'`` outcomes; the persisted
  ``comment_sync_state`` row travels in ``sync_state``.
* Analysis triggers with zero stored comments answer 200 with
  ``status='no_comments'`` and persist NOTHING (an engine run over
  zero comments would have no owner to persist against and nothing
  to say).
* Draft approval is a LOCAL decision only: posting is NOT WIRED in
  this round.  Every draft view carries ``posted=False`` and
  ``posting='not_wired'`` (stamped by the engagement service's
  ``draft_view``), and moderation views carry
  ``action_executed=False`` — suggested actions are labels for a
  human reviewer, never executed.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any, Optional

from app.comments import engagement
from app.comments.store import (
    CommentsNotFoundError,
    CommentsScopeError,
    CommentsStateError,
    CommentsStore,
)
from app.errors import ApiError

logger = logging.getLogger(__name__)

__all__ = [
    "resolve_channel_id",
    "sync_video",
    "sync_status",
    "sync_channel",
    "list_comment_videos",
    "rollups",
    "analyse_channel_or_video",
    "list_video_comments",
    "video_analysis",
    "channel_analysis",
    "analyse_video",
    "analyse_channel",
    "generate_draft",
    "list_drafts",
    "get_draft",
    "decide_draft",
    "edit_draft",
    "list_moderation",
    "decide_moderation",
    "POSTING_NOT_WIRED",
]

#: The posting boundary, echoed on sync/analysis payloads too so a
#: client can never mistake any part of this surface for a YouTube
#: write path (draft views carry it per-row via the engagement layer).
POSTING_NOT_WIRED = "not_wired"

# OAuth client credential refs (app.routers.channels' connect flow
# stores the token blobs against these via the same secret store).
_CLIENT_ID_REF = "youtube/client_id"
_CLIENT_SECRET_REF = "youtube/client_secret"


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def resolve_channel_id(channel_id: str) -> str:
    """Owner-channel resolution (404 ``channel_not_found``), delegated
    to the engagement module's identical helper (style_lab precedent:
    internal id or YouTube channel id accepted)."""
    return engagement.resolve_channel_id(channel_id)


def _store() -> CommentsStore:
    return engagement.default_store()


def _engagement_service() -> engagement.CommentEngagementService:
    return engagement.default_service()


def _not_found(code: str, what: str, ident: Any) -> ApiError:
    return ApiError(404, code, f"No {what} with id '{ident}'.")


# ---------------------------------------------------------------------------
# Sync (fetch worker seam)
# ---------------------------------------------------------------------------


class _KeyringSecretStore:
    """Sync SecretStore over the OS keyring (mirrors
    ``app.routers.channels._KeyringSecretStore``)."""

    def get(self, ref: str) -> Optional[str]:
        from app import secrets_store

        return secrets_store.get_secret(ref)

    def put(self, ref: str, value: str) -> None:
        from app import secrets_store

        secrets_store.set_secret(ref, value)

    def delete(self, ref: str) -> None:
        from app import secrets_store

        secrets_store.delete_secret(ref)


def _secret_store(request: Any):
    """Resolve the sync SecretStore (request override -> env -> keyring),
    mirroring ``app.routers.channels._secret_store``."""
    override = getattr(getattr(request, "app", None), "state", None)
    override = getattr(override, "youtube_secret_store", None)
    if override is not None:
        return override
    if os.environ.get("YTA_SECRET_BACKEND", "").strip().lower() == "env":
        from app.providers.secrets_env import EnvSecretStore

        return EnvSecretStore()
    return _KeyringSecretStore()


def _build_token_manager(request: Any):
    """Best-effort OAuth TokenManager for the sync credential gate.

    Returns ``None`` whenever the OAuth client pair is not configured
    or anything fails — the sync then relies on the provider's
    API-key path and, failing that, honestly reports
    ``not_connected``.  NEVER raises and never logs secret material.
    """
    if request is None:
        return None
    try:
        store = _secret_store(request)
        client_id = store.get(_CLIENT_ID_REF)
        client_secret = store.get(_CLIENT_SECRET_REF)
        if not (client_id and str(client_id).strip()):
            return None
        if not (client_secret and str(client_secret).strip()):
            return None
        from app.providers.youtube.tokens import TokenManager

        return TokenManager(
            store, client_id=str(client_id), client_secret=str(client_secret)
        )
    except Exception:  # noqa: BLE001 — degrade to the API-key path
        logger.warning("comments sync: OAuth token manager unavailable")
        return None


async def _sync_one(channel_pk: str, video_id: str,
                    request: Any = None) -> dict:
    from app import db as db_module
    from app.comments.service import sync_video_comments

    result = await sync_video_comments(
        channel_pk,
        str(video_id),
        store=_store(),
        conn_factory=db_module.get_connection,
        token_manager=_build_token_manager(request),
    )
    payload = dataclasses.asdict(result)
    payload["sync_status"] = result.sync_state_status
    return payload


async def sync_video(channel_id: str, video_id: str, request: Any = None) -> dict:
    """Trigger a comment sync for one video; return the typed result.

    Delegates entirely to :func:`app.comments.service.sync_video_comments`.
    Every outcome — ``fetched`` / ``not_connected`` / typed errors —
    is a 200 payload (the codebase's honest-state convention; see the
    module docstring).  The only failure raised is an unknown owner
    channel (404 ``channel_not_found``).
    """
    channel_pk = resolve_channel_id(channel_id)
    return await _sync_one(channel_pk, str(video_id), request)


def sync_status(channel_id: str, video_id: str) -> dict:
    """The persisted sync state for one (channel, video).

    A never-synced video has no row; the store synthesises the honest
    ``status='never_synced'`` state (``exists=False``)."""
    channel_pk = resolve_channel_id(channel_id)
    return _store().get_sync_state(channel_pk, str(video_id))


# ---------------------------------------------------------------------------
# Channel-level aggregates (the Comments screen's contract:
# per-video sync rows, sync-all, rollups overview)
# ---------------------------------------------------------------------------


def _channel_video_index(channel_pk: str) -> dict[str, Optional[str]]:
    """YouTube video ids known for a channel -> published title.

    Sources (all owner-scoped): ``publish_records`` joined through
    ``projects.channel_id``, ``projects.youtube_video_id``, stored
    ``comment_sync_state`` rows and distinct stored-comment video
    ids.  Best-effort: any missing table simply contributes nothing.
    """
    from app import db as db_module

    index: dict[str, Optional[str]] = {}
    conn = db_module.get_connection()
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "publish_records" in tables and "projects" in tables:
            for vid, title in conn.execute(
                "SELECT pr.youtube_video_id, pr.title FROM publish_records pr"
                " JOIN projects p ON p.id = pr.project_id"
                " WHERE p.channel_id = ?",
                (channel_pk,),
            ):
                if vid:
                    index[str(vid)] = title
        if "projects" in tables:
            for (vid,) in conn.execute(
                "SELECT youtube_video_id FROM projects"
                " WHERE channel_id = ? AND youtube_video_id IS NOT NULL",
                (channel_pk,),
            ):
                if vid:
                    index.setdefault(str(vid), None)
    except Exception:  # noqa: BLE001 — discovery is best-effort
        logger.warning("comments: published-video discovery failed")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    store = _store()
    for state in store.list_sync_states(channel_pk):
        index.setdefault(str(state["video_id"]), None)
    # Distinct video ids among stored comments (no store helper; the
    # sync-state union above covers synced videos — this adds any
    # video whose comments exist without a state row).
    conn = db_module.get_connection()
    try:
        for (vid,) in conn.execute(
            "SELECT DISTINCT video_id FROM comments WHERE channel_id = ?",
            (channel_pk,),
        ):
            if vid:
                index.setdefault(str(vid), None)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return index


def list_comment_videos(channel_id: str) -> dict:
    """Per-video sync rows for the channel's Comments screen:
    ``{video_id, title, sync_status, comment_count, last_synced_at,
    error_reason, analysis_status}`` per known video."""
    channel_pk = resolve_channel_id(channel_id)
    store = _store()
    items = []
    for video_id, title in _channel_video_index(channel_pk).items():
        state = store.get_sync_state(channel_pk, video_id)
        count = (
            state["comment_count"] if state["exists"]
            else store.count_comments(channel_pk, video_id)
        )
        analysed = store.latest_analysis(
            channel_pk, scope="video", video_id=video_id
        )
        items.append({
            "video_id": video_id,
            "title": title,
            "sync_status": state["status"],
            "status": state["status"],
            "comment_count": count,
            "last_synced_at": state["last_synced_at"],
            "error_reason": state["error_reason"],
            "analysis_status": "analysed" if analysed else "not_analysed",
        })
    items.sort(key=lambda r: (r["title"] or r["video_id"]))
    return {"items": items, "next_cursor": None}


async def sync_channel(channel_id: str, video_id: Optional[str] = None,
                       request: Any = None) -> dict:
    """Sync one video (``video_id`` given) or every known video of
    the channel, sequentially; per-video typed results are returned
    verbatim under ``results`` and the aggregate ``status`` is honest:
    ``no_videos`` when the channel has no known videos,
    ``not_connected`` when nothing could connect, ``fetched`` when
    at least one video fetched, else ``error``."""
    channel_pk = resolve_channel_id(channel_id)
    if video_id:
        payload = await _sync_one(channel_pk, str(video_id), request)
        return {**payload, "results": [payload], "video_count": 1}
    videos = list(_channel_video_index(channel_pk).keys())
    if not videos:
        return {
            "status": "no_videos",
            "channel_id": channel_pk,
            "video_id": None,
            "video_count": 0,
            "comment_count": _store().count_comments(channel_pk),
            "results": [],
        }
    results = [await _sync_one(channel_pk, vid, request) for vid in videos]
    statuses = {r["status"] for r in results}
    if statuses == {"fetched"}:
        aggregate = "fetched"
    elif "fetched" in statuses:
        aggregate = "fetched"  # partial success; details per-video
    elif statuses == {"not_connected"}:
        aggregate = "not_connected"
    else:
        aggregate = "error"
    return {
        "status": aggregate,
        "channel_id": channel_pk,
        "video_id": None,
        "video_count": len(results),
        "comment_count": sum(int(r.get("total_stored") or 0) for r in results),
        "results": results,
    }


def rollups(channel_id: str) -> dict:
    """The channel rollup + the latest rollup per analysed video
    (persisted rows only; ``status='not_analysed'`` + nulls when no
    analysis has ever been persisted — never fabricated)."""
    channel_pk = resolve_channel_id(channel_id)
    store = _store()
    channel_row = store.latest_analysis(channel_pk, scope="channel")
    latest_by_video: dict[str, dict] = {}
    for row in store.list_analyses(channel_pk, scope="video"):
        vid = row.get("video_id")
        if vid and str(vid) not in latest_by_video:
            latest_by_video[str(vid)] = row
    videos = list(latest_by_video.values())
    return {
        "status": "analysed" if (channel_row or videos) else "not_analysed",
        "channel_id": channel_pk,
        "channel": channel_row,
        "videos": videos,
    }


# ---------------------------------------------------------------------------
# Stored comments (paginated, flags attached)
# ---------------------------------------------------------------------------


def _flags_by_comment(store: CommentsStore, channel_pk: str,
                      video_id: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for flag in store.list_moderation_flags(channel_pk, video_id=video_id):
        grouped.setdefault(str(flag["comment_id"]), []).append(flag)
    return grouped


def list_video_comments(channel_id: str, video_id: str, *,
                        limit: int = 50, offset: int = 0,
                        top_level_only: bool = False) -> dict:
    """One page of stored comments for a video, newest first, each
    with its moderation flags attached (``flags`` / ``flag_count``)."""
    channel_pk = resolve_channel_id(channel_id)
    store = _store()
    total = store.count_comments(channel_pk, str(video_id))
    rows = store.list_comments(
        channel_pk, str(video_id),
        top_level_only=top_level_only, limit=limit, offset=offset,
    )
    flags = _flags_by_comment(store, channel_pk, str(video_id))
    items = []
    for row in rows:
        view = dict(row)
        view["flags"] = flags.get(str(row["id"]), [])
        view["flag_count"] = len(view["flags"])
        items.append(view)
    next_cursor = (
        str(offset + len(items)) if offset + len(items) < total else None
    )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_cursor": next_cursor,
        "channel_id": channel_pk,
        "video_id": str(video_id),
    }


# ---------------------------------------------------------------------------
# Analysis rollups (reads = store; triggers = analysis engine)
# ---------------------------------------------------------------------------


def _analysis_payload(channel_pk: str, row: Optional[dict], *,
                      scope: str, video_id: Optional[str]) -> dict:
    if row is None:
        return {
            "status": "not_analysed",
            "state": "not_analysed",
            "analysis": None,
            "channel_id": channel_pk,
            "scope": scope,
            "video_id": video_id,
        }
    return {
        "status": "analysed",
        "state": "analysed",
        "analysis": row,
        "channel_id": channel_pk,
        "scope": scope,
        "video_id": video_id,
    }


def video_analysis(channel_id: str, video_id: str) -> dict:
    """Latest persisted per-video rollup, or ``not_analysed``."""
    channel_pk = resolve_channel_id(channel_id)
    row = _store().latest_analysis(
        channel_pk, scope="video", video_id=str(video_id)
    )
    return _analysis_payload(channel_pk, row, scope="video", video_id=str(video_id))


def channel_analysis(channel_id: str) -> dict:
    """Latest persisted per-channel rollup, or ``not_analysed``."""
    channel_pk = resolve_channel_id(channel_id)
    row = _store().latest_analysis(channel_pk, scope="channel")
    return _analysis_payload(channel_pk, row, scope="channel", video_id=None)


def _analysis_engine():
    from app.comment_intel import CommentAnalysisEngine

    return CommentAnalysisEngine(llm=engagement.default_llm())


async def _run_analysis(channel_pk: str, scope: str,
                        video_id: Optional[str]) -> dict:
    store = _store()
    count = store.count_comments(
        channel_pk, str(video_id) if video_id else None
    )
    if count == 0:
        # Honest no-op: nothing stored, nothing persisted, no numbers.
        return {
            "status": "no_comments",
            "state": "no_comments",
            "analysis": None,
            "comment_count": 0,
            "channel_id": channel_pk,
            "scope": scope,
            "video_id": video_id,
        }
    from app.comment_intel import CommentAnalysisError

    engine = _analysis_engine()
    try:
        if scope == "video":
            rollup, report = await engine.analyse_stored_video(
                store, channel_pk, str(video_id)
            )
        else:
            rollup, report = await engine.analyse_stored_channel(
                store, channel_pk
            )
    except CommentAnalysisError as exc:
        raise ApiError(502, "analysis_failed", str(exc)) from exc
    row = store.latest_analysis(
        channel_pk, scope=scope,
        video_id=str(video_id) if video_id else None,
    )
    payload = _analysis_payload(channel_pk, row, scope=scope, video_id=video_id)
    payload["status"] = "analysed"
    payload["rollup"] = rollup.model_dump(mode="json")
    payload["engine"] = rollup.provenance.engine
    payload["model_id"] = rollup.provenance.model
    payload["comment_count"] = rollup.comment_count
    payload["persist_report"] = report
    return payload


async def analyse_video(channel_id: str, video_id: str) -> dict:
    """Trigger analysis of one video's STORED comments (the persisted
    rollup is returned; spam flags land in the moderation queue)."""
    channel_pk = resolve_channel_id(channel_id)
    return await _run_analysis(channel_pk, "video", str(video_id))


async def analyse_channel(channel_id: str) -> dict:
    """Trigger the channel rollup over ALL stored comments."""
    channel_pk = resolve_channel_id(channel_id)
    return await _run_analysis(channel_pk, "channel", None)


async def analyse_channel_or_video(channel_id: str,
                                   video_id: Optional[str] = None) -> dict:
    """The Comments screen's analysis trigger: one video when
    ``video_id`` is given, the whole channel otherwise."""
    if video_id:
        return await analyse_video(channel_id, str(video_id))
    return await analyse_channel(channel_id)


# ---------------------------------------------------------------------------
# Drafts (engagement service; approval is LOCAL — posting not wired)
# ---------------------------------------------------------------------------


def _map_engagement_error(exc: Exception, *, code: str, what: str,
                            ident: Any) -> ApiError:
    if isinstance(exc, (CommentsNotFoundError, CommentsScopeError)):
        # Cross-channel existence must not leak: same 404 as unknown.
        return _not_found(code, what, ident)
    if isinstance(exc, (CommentsStateError, engagement.IllegalTransitionError)):
        return ApiError(409, "illegal_transition", str(exc))
    if isinstance(exc, engagement.DraftGenerationError):
        return ApiError(502, "draft_generation_failed", str(exc))
    if isinstance(exc, ValueError):
        return ApiError(422, "invalid_request", str(exc))
    return ApiError(500, "internal_error", "Internal server error")


async def generate_draft(channel_id: str, comment_id: str) -> dict:
    """Generate a reply draft for one stored comment (201 by the router).

    The draft is a LOCAL ``proposed`` row; its view carries
    ``status`` + ``provenance`` + ``posted=False`` /
    ``posting='not_wired'``."""
    channel_pk = resolve_channel_id(channel_id)
    try:
        return await _engagement_service().generate_draft(
            comment_id, channel_id=channel_pk
        )
    except Exception as exc:
        raise _map_engagement_error(
            exc, code="comment_not_found", what="comment", ident=comment_id
        ) from exc


def list_drafts(channel_id: str, *, status: Optional[str] = None,
                video_id: Optional[str] = None,
                comment_id: Optional[str] = None) -> dict:
    channel_pk = resolve_channel_id(channel_id)
    try:
        items = _engagement_service().list_drafts(
            channel_pk, status=status, video_id=video_id,
            comment_id=comment_id,
        )
    except ValueError as exc:
        raise ApiError(422, "invalid_draft_status", str(exc)) from exc
    return {"items": items, "next_cursor": None}


def get_draft(channel_id: str, draft_id: str) -> dict:
    channel_pk = resolve_channel_id(channel_id)
    try:
        return _engagement_service().get_draft(draft_id, channel_id=channel_pk)
    except Exception as exc:
        raise _map_engagement_error(
            exc, code="draft_not_found", what="comment draft", ident=draft_id
        ) from exc


def decide_draft(channel_id: str, draft_id: str, decision: str,
                 decided_by: Optional[str] = None) -> dict:
    """Approve / reject / discard a proposed draft — a LOCAL
    decision only.  ``discarded`` is a terminal state distinct from
    ``rejected``.

    Approval does NOT post the reply: posting is not wired in this
    round, and the returned view still carries ``posted=False`` /
    ``posting='not_wired'``."""
    channel_pk = resolve_channel_id(channel_id)
    try:
        return _engagement_service().decide_draft(
            draft_id, decision, channel_id=channel_pk,
            decided_by=decided_by,
        )
    except Exception as exc:
        raise _map_engagement_error(
            exc, code="draft_not_found", what="comment draft", ident=draft_id
        ) from exc


def edit_draft(channel_id: str, draft_id: str, draft_text: str,
               edited_by: Optional[str] = None) -> dict:
    """Edit a ``proposed`` draft's text — a LOCAL update only.

    The edit is recorded in the draft's provenance (who / when /
    previous + new text) by the store.  Editing a terminal draft
    answers 409; nothing is ever posted."""
    channel_pk = resolve_channel_id(channel_id)
    try:
        return _engagement_service().edit_draft(
            draft_id, draft_text, channel_id=channel_pk,
            edited_by=edited_by,
        )
    except Exception as exc:
        raise _map_engagement_error(
            exc, code="draft_not_found", what="comment draft", ident=draft_id
        ) from exc


# ---------------------------------------------------------------------------
# Moderation queue (flags + suggested actions; decisions recorded LOCALLY)
# ---------------------------------------------------------------------------


def list_moderation(channel_id: str, *, status: Optional[str] = "pending",
                    flag_kind: Optional[str] = None,
                    video_id: Optional[str] = None) -> dict:
    """Flagged comments + suggested-action LABELS for human review.

    ``status=None`` lists all flags; the default is the actionable
    ``pending`` queue.  Views carry the flagged comment's text/author,
    ``suggested_action_label`` and ``action_executed=False``."""
    channel_pk = resolve_channel_id(channel_id)
    try:
        items = _engagement_service().list_moderation_queue(
            channel_pk, status=status, flag_kind=flag_kind,
            video_id=video_id,
        )
    except ValueError as exc:
        raise ApiError(422, "invalid_moderation_filter", str(exc)) from exc
    return {"items": items, "next_cursor": None}


def decide_moderation(channel_id: str, flag_id: str, decision: str,
                      decided_by: Optional[str] = None) -> dict:
    """Record a LOCAL human decision on a flag.

    ``decision`` is ``dismiss`` or ``reviewed`` (the engagement
    service's ``mark_reviewed``; ``actioned`` is also accepted as the
    stored-status spelling).  Recording a decision NEVER executes the
    suggested action against YouTube — the returned view carries
    ``action_executed=False``."""
    channel_pk = resolve_channel_id(channel_id)
    normalised = {"reviewed": "mark_reviewed", "actioned": "mark_reviewed"}
    decision = normalised.get(decision, decision)
    try:
        return _engagement_service().decide_moderation_flag(
            flag_id, decision, channel_id=channel_pk,
            decided_by=decided_by,
        )
    except Exception as exc:
        if isinstance(exc, ValueError) and not isinstance(
            exc, engagement.IllegalTransitionError
        ):
            raise ApiError(
                422, "invalid_moderation_decision", str(exc)
            ) from exc
        raise _map_engagement_error(
            exc, code="moderation_flag_not_found",
            what="moderation flag", ident=flag_id,
        ) from exc
