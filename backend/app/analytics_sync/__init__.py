"""Analytics ingestion — sync service, scheduler poller, manual trigger.

Round 5 (Growth + Learning), Analytics Ingestion team. This package is
the *sync layer* that sits between the transport client
(:class:`app.providers.youtube.analytics.YouTubeAnalyticsClient` —
typed ``ok`` / ``not_connected`` / ``failed`` results, 1 shared-bucket
quota unit per call) and the analytics storage tables
(``analytics_snapshots`` per video, ``channel_metric_snapshots`` per
channel — idempotent upserts, one row per UTC day + snapshot kind:
the idempotency day is the store's ``capture_day`` basis,
:func:`app.analytics.schema.capture_day_for`, in BOTH layers —
R5-002 closure. Pacific time governs only the shared quota bucket
and the Analytics API query dates, never snapshot identity).

It owns:

* :mod:`app.analytics_sync.schema` — runtime DDL for the sync
  bookkeeping only (``analytics_sync_runs`` /
  ``analytics_sync_state``). The analytics data tables
  (``analytics_snapshots`` / ``channel_metric_snapshots``) belong to
  the storage team (``app.analytics.schema``, migration 0007) and
  the sync writes them exclusively through
  :class:`app.analytics.store.AnalyticsStore` — the single canonical
  shape (R5-ANALYTICS-001, settled);
* :mod:`app.analytics_sync.service` — :class:`AnalyticsSyncService`
  (per-channel sync, quota-aware against the shared daily bucket read
  through :mod:`app.quota_view`), :class:`AnalyticsSyncPoller` (the
  scheduler hook hosted by ``app.execution.service`` exactly like the
  Style Lab claimer), and the API-facing helpers the router calls.

Honesty contract (mirrors the sibling Round 5 modules): a channel
that is not connected produces a *recorded* ``not_connected`` sync run
with zero snapshot writes; quota exhaustion stops a sync cleanly with
partial progress recorded; every failure is recorded with a reason —
never a silent no-op, never an exception escaping the sync loop.
"""

from __future__ import annotations

__all__: list[str] = []
