"""Analytics sync endpoints (Round 5 — Analytics Ingestion).

Routes (nested under the OWNER channel, following
``app.routers.style_lab`` conventions)::

    POST /api/v1/channels/{channel_id}/analytics/sync
    GET  /api/v1/channels/{channel_id}/analytics/sync-status

* The POST is the MANUAL TRIGGER: it runs one per-channel sync now
  (``trigger='manual'``) through
  :class:`app.analytics_sync.service.AnalyticsSyncService` and returns
  the settled run summary. A sync outcome is data, not an HTTP error:
  a not-connected channel answers 200 with ``status='not_connected'``
  + ``reason`` (and the run is durably recorded), exactly like the
  Style Lab metadata fetch's honest ``metadata_unavailable``.
* The GET is the freshness surface: ``last_synced_at`` /
  ``last_attempt_at`` / ``last_status`` / ``last_reason`` for the
  channel (``last_status='never_synced'`` before the first attempt).

Owner scoping: ``{channel_id}`` accepts the internal channel id or
the YouTube channel id (the channels-router convention); an unknown
channel answers 404 ``channel_not_found`` — never a cross-channel
leak, since every read/write is keyed by the resolved channel row.

The endpoints are plain ``def`` handlers (FastAPI runs them in its
threadpool): the sync is blocking SQLite + client work, mirroring how
the execution layer drives the same service via ``asyncio.to_thread``.

Test/wiring overrides (``app.state``): ``analytics_sync_service`` (a
ready service) wins; else ``analytics_client`` (an injected client)
is used with a fresh service; else the production default service
(real analytics client — ``not_connected`` until a channel connects).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.analytics_sync import service as sync_service

router = APIRouter(prefix="/api/v1/channels", tags=["analytics_sync"])


def _conn_factory_for(request: Request):
    """conn_factory over THIS app's database (``app.state.db_path``,
    set by ``create_app``) — never the process-default path, so the
    router always talks to the database the app was built with."""
    from app import db as db_module

    db_path = getattr(request.app.state, "db_path", None)
    if db_path is not None:
        return lambda: db_module.get_connection(db_path)  # noqa: E731
    return db_module.get_connection


def _service_for(request: Request) -> sync_service.AnalyticsSyncService:
    override = getattr(request.app.state, "analytics_sync_service", None)
    if override is not None:
        return override
    client = getattr(request.app.state, "analytics_client", None)
    if client is not None:
        return sync_service.AnalyticsSyncService(
            _conn_factory_for(request), client=client
        )
    return sync_service.AnalyticsSyncService(
        _conn_factory_for(request),
        client_factory=sync_service.build_default_client,
    )


@router.post("/{channel_id}/analytics/sync")
def trigger_sync(channel_id: str, request: Request) -> dict:
    """Run one analytics sync for this channel now (manual trigger)."""
    return sync_service.trigger_manual_sync(
        channel_id, service=_service_for(request)
    )


@router.get("/{channel_id}/analytics/sync-status")
def sync_status(channel_id: str, request: Request) -> dict:
    """Freshness + last outcome for this channel's analytics sync."""
    return sync_service.get_channel_sync_status(
        channel_id, service=_service_for(request)
    )
