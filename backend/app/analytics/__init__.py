"""Analytics ingestion — snapshot storage (Round 5, Growth + Learning).

This package holds the persistence layer only:

* :mod:`app.analytics.schema` — runtime DDL + lazy schema creation
  for ``analytics_snapshots`` (evolved in place — the table dates
  from the section-15 initial schema) and the new
  ``channel_metric_snapshots`` table, kept in sync with Alembic
  migration ``0007_analytics``;
* :mod:`app.analytics.store` — the owner-scoped store (idempotent
  per-day upserts, per-channel/video reads, YPP channel metrics,
  ``last_synced_at`` derivation).

The YouTube API client, the sync service and the HTTP routes are
sibling Round 5 workstreams and deliberately not part of this
package.
"""

from __future__ import annotations

__all__: list[str] = []
