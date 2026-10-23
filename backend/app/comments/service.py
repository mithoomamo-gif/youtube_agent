"""Comments Intelligence — comment sync service (Round 5, Agent 28).

:func:`sync_video_comments` is the sync entry point: it fetches every
comment thread for one of the user's own videos through the read-only
provider (:mod:`app.providers.youtube.comments`) and upserts them
idempotently into the comments store
(:class:`app.comments.store.CommentsStore`), recording the outcome in
``comment_sync_state``.

Sync semantics
--------------
* **Idempotent upsert.**  Comments are keyed by the YouTube comment id
  (the ``comments`` table PRIMARY KEY).  A re-sync inserts only NEW
  comments and refreshes the mutable fields of existing ones (text,
  like/reply counts, ``fetched_at``) via the store's upsert — the row
  count never grows on a re-sync of unchanged data.
  ``inserted_count`` / ``updated_count`` on the result are computed
  honestly: the set of already-stored ids for the (channel, video) is
  read BEFORE the upsert and compared with the fetched ids.
* **All-or-nothing per sync.**  The fetch layer returns comments only
  on a complete, successful pagination; on any failure NOTHING is
  upserted (no partial pages in the store) and the sync state records
  the typed failure instead.
* **Honest typed states.**  The store's ``comment_sync_state.status``
  vocabulary is ``synced`` / ``not_connected`` / ``error`` (schema
  CHECK).  Mapping from the fetch outcome:

  ===========================  ==================  ==========================
  fetch status                 sync-state status   ``error_reason``
  ===========================  ==================  ==========================
  ``fetched``                  ``synced``          (cleared)
  ``not_connected``            ``not_connected``   the gate reason
                               (``no_youtube_credential`` /
                               ``oauth_token_unavailable``)
  ``comments_disabled``        ``error``           ``comments_disabled``
  ``video_not_found``          ``error``           ``video_not_found``
  ``auth_failed``              ``error``           ``authentication_failed``
  ``quota_exceeded``           ``error``           ``quota_exceeded``
  ``fetch_failed``             ``error``           the fetch reason verbatim
  ===========================  ==================  ==========================

  The richer fetch status is preserved verbatim on the returned
  :class:`CommentSyncResult` (``status`` / ``reason``) even where the
  stored vocabulary collapses it into ``error`` + ``error_reason``.
* **Never raised raw, never fabricated.**  Fetch failures are typed
  results, not exceptions; a store failure during the upsert is
  caught and reported as ``status="fetch_failed"`` /
  ``reason="store_error"`` with the sync state left untouched (the
  state write itself is best-effort after that point).  On failure
  the stored ``comment_count`` keeps its previous value — counts are
  only ever written from a successful upsert or a live store count.
* **Read-only.**  This service performs no YouTube write call of any
  kind; the provider it drives only ever issues
  ``commentThreads.list`` GETs.

Channel / credential wiring
---------------------------
``channel_id`` is the INTERNAL ``channels.id`` (the store's owner
scope).  The OAuth token manager, however, keys credential blobs by
the channel's EXTERNAL YouTube channel id, so ``oauth_channel_id``
should be that external id.  When it is not supplied, the service
tries to resolve it from the ``channels`` table via ``conn_factory``
(best-effort: any failure leaves it as ``channel_id`` itself, which
is correct when the caller already passed the external id).  An API
key (``secret_provider``) needs no channel resolution at all — see
the provider's credential gate.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from app.providers.youtube import comments as comments_provider

__all__ = [
    "CommentSyncResult",
    "sync_video_comments",
    "sync_video_comments_sync",
    "STATUS_SYNCED",
    "STATUS_NOT_CONNECTED",
    "STATUS_ERROR",
]

#: comment_sync_state.status values written by this service
#: (mirror app.comments.store.SYNC_STATUSES).
STATUS_SYNCED = "synced"
STATUS_NOT_CONNECTED = "not_connected"
STATUS_ERROR = "error"


@dataclass
class CommentSyncResult:
    """Outcome of one :func:`sync_video_comments` call.

    ``status`` is the RICH fetch-level status (``fetched`` /
    ``not_connected`` / ``comments_disabled`` / ``video_not_found`` /
    ``auth_failed`` / ``quota_exceeded`` / ``fetch_failed``);
    ``sync_state_status`` is what was written to
    ``comment_sync_state`` (``synced`` / ``not_connected`` /
    ``error``).  Counts are exact: ``fetched_count`` records fetched
    in this attempt, ``inserted_count`` genuinely new rows,
    ``updated_count`` pre-existing rows refreshed, ``total_stored``
    the store's live count for the (channel, video) after the sync.
    """

    status: str
    channel_id: str
    video_id: str
    fetched_count: int = 0
    inserted_count: int = 0
    updated_count: int = 0
    total_stored: int = 0
    pages_fetched: int = 0
    quota_units_used: int = 0
    reason: str | None = None
    detail: str = ""
    sync_state_status: str | None = None
    sync_state: dict[str, Any] | None = field(default=None)


def _default_store(conn_factory: Any) -> Any:
    from app.comments.store import CommentsStore

    if conn_factory is not None:
        return CommentsStore(conn_factory)
    from functools import partial

    from app import db as db_module

    return CommentsStore(partial(db_module.connect, db_module.default_db_path()))


def _resolve_external_channel_id(conn_factory: Any, channel_id: str) -> str:
    """Best-effort internal -> external channel id resolution.

    Returns ``channel_id`` unchanged on ANY failure (no factory, no
    row, DB error) — resolution must never break a sync that could
    still succeed via the API-key credential path.
    """
    if conn_factory is None:
        return channel_id
    conn = None
    should_close = False
    try:
        if hasattr(conn_factory, "execute"):
            conn = conn_factory
        elif callable(conn_factory):
            conn = conn_factory()
            should_close = True
        if conn is None:
            return channel_id
        row = conn.execute(
            "SELECT youtube_channel_id FROM channels WHERE id = ?",
            (str(channel_id),),
        ).fetchone()
        if row is not None:
            value = row[0]
            if isinstance(value, str) and value:
                return value
        return channel_id
    except Exception:
        return channel_id
    finally:
        if should_close and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _stored_ids(store: Any, channel_id: str, video_id: str) -> set[str]:
    """Ids currently stored for (channel, video) — pre-upsert snapshot."""
    try:
        rows = store.list_comments(channel_id, video_id)
    except Exception:
        return set()
    return {str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")}


async def sync_video_comments(
    channel_id: str,
    video_id: str,
    *,
    store: Any = None,
    conn_factory: Any = None,
    project_id: str | None = None,
    secret_provider: Any = None,
    token_manager: Any = None,
    oauth_channel_id: str | None = None,
    quota_ledger: Any = None,
    transport: Any = None,
    timeout_s: float = 30.0,
) -> CommentSyncResult:
    """Sync all comments for one video into the store (idempotent).

    Never raises for credential / transport / HTTP / API failures —
    they are returned as typed :class:`CommentSyncResult` values and
    recorded in ``comment_sync_state``.
    """
    channel_id = str(channel_id)
    video_id = str(video_id)
    if store is None:
        store = _default_store(conn_factory)
    if oauth_channel_id is None and token_manager is not None:
        oauth_channel_id = _resolve_external_channel_id(conn_factory, channel_id)

    fetch = await comments_provider.fetch_video_comments(
        video_id,
        secret_provider=secret_provider,
        token_manager=token_manager,
        oauth_channel_id=oauth_channel_id,
        quota_ledger=quota_ledger,
        quota_channel_id=channel_id,
        transport=transport,
        timeout_s=timeout_s,
    )

    result = CommentSyncResult(
        status=fetch.status,
        channel_id=channel_id,
        video_id=video_id,
        pages_fetched=fetch.pages_fetched,
        quota_units_used=fetch.quota_units_used,
        reason=fetch.reason,
        detail=fetch.detail,
    )

    # -- failure paths: record the typed state, upsert NOTHING ---------
    if fetch.status != comments_provider.STATUS_FETCHED:
        if fetch.status == comments_provider.STATUS_NOT_CONNECTED:
            state_status = STATUS_NOT_CONNECTED
            error_reason = fetch.reason or "no_youtube_credential"
        else:
            state_status = STATUS_ERROR
            error_reason = fetch.reason or fetch.status
        try:
            result.total_stored = int(store.count_comments(channel_id, video_id))
        except Exception:
            result.total_stored = 0
        try:
            state = store.set_sync_state(
                channel_id,
                video_id,
                state_status,
                error_reason=error_reason,
                project_id=project_id,
            )
            result.sync_state = state
            result.sync_state_status = state.get("status", state_status)
        except Exception:
            # State recording is best-effort; the typed result stands.
            result.sync_state_status = state_status
        return result

    # -- success: idempotent upsert, then mark synced -------------------
    fetched = [dataclasses.asdict(c) for c in fetch.comments]
    result.fetched_count = len(fetched)
    before_ids = _stored_ids(store, channel_id, video_id)
    fetched_ids = {str(c["id"]) for c in fetched}
    result.inserted_count = len(fetched_ids - before_ids)
    result.updated_count = len(fetched_ids & before_ids)
    try:
        store.upsert_comments(channel_id, fetched, video_id=video_id)
        result.total_stored = int(store.count_comments(channel_id, video_id))
    except Exception as exc:  # store failure — typed, never raised raw
        result.status = comments_provider.STATUS_FETCH_FAILED
        result.reason = "store_error"
        result.detail = f"comment upsert failed: {type(exc).__name__}"
        result.inserted_count = 0
        result.updated_count = 0
        return result
    try:
        state = store.set_sync_state(
            channel_id,
            video_id,
            STATUS_SYNCED,
            comment_count=result.total_stored,
            project_id=project_id,
        )
        result.sync_state = state
        result.sync_state_status = state.get("status", STATUS_SYNCED)
    except Exception:
        result.sync_state_status = STATUS_SYNCED
    return result


def sync_video_comments_sync(channel_id: str, video_id: str, **kwargs: Any) -> CommentSyncResult:
    """Sync facade over :func:`sync_video_comments`.

    Drives the coroutine via ``app.integration.run_loop.run_coroutine``
    (the codebase's single loop bridge — the metadata precedent);
    async callers must ``await sync_video_comments(...)`` directly.
    """
    from app.integration.run_loop import run_coroutine

    return run_coroutine(lambda: sync_video_comments(channel_id, video_id, **kwargs))
