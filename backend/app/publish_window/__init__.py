"""Publish-window suggestions (Round 5, Learning Loop).

Roadmap authority (MASTER_ROADMAP §3.10, "Best-time modes"): the
**Auto (Analytics)** scheduling mode becomes eligible only at
**≥10 published videos AND ≥28 days of analytics data** for the
channel.  Agent 29 computes windows from the channel's own
first-24h velocity by historical publish hour; windows are
*proposed as a suggestion* and are **never silently applied**.

This package implements exactly that suggestion computation:

* :mod:`app.publish_window.adapter` — the analytics read seam
  (a real SQLite adapter over ``publish_records`` ⨝ ``projects``
  and ``analytics_snapshots``, plus an in-memory source for
  synthetic fixtures);
* :mod:`app.publish_window.service` — eligibility gate, timezone-
  correct hour/weekday aggregation, ranking with evidence, and
  optional persistence of the suggestion as a ``publish_window``
  learning rule via the Learning Store sibling
  (:class:`app.learning.store.LearningStore`).

Hard boundary: this package **never writes to any schedule**.
It contains no import of, and no SQL against, ``schedules`` /
``schedule_runs``; the only write it can perform is creating a
*candidate* learning rule when a store is supplied — activation
is a separate, explicit step owned by the store's callers.
"""

from __future__ import annotations

from app.publish_window.adapter import (
    ChannelPublishData,
    InMemoryPublishWindowSource,
    PublishedVideoVelocity,
    PublishWindowSource,
    SqlitePublishWindowSource,
)
from app.publish_window.service import (
    MIN_ANALYTICS_DAYS,
    MIN_PUBLISHED_VIDEOS,
    RULE_KIND,
    PublishWindowService,
)

__all__ = [
    "ChannelPublishData",
    "InMemoryPublishWindowSource",
    "MIN_ANALYTICS_DAYS",
    "MIN_PUBLISHED_VIDEOS",
    "PublishedVideoVelocity",
    "PublishWindowService",
    "PublishWindowSource",
    "RULE_KIND",
    "SqlitePublishWindowSource",
    "get_publish_window_suggestion",
]


def get_publish_window_suggestion(channel_id, *, conn_factory=None):
    """API seam consumed by ``app.learning.service`` (R5L-003 fix).

    Builds the REAL :class:`PublishWindowService` over the real
    SQLite analytics adapter (:class:`SqlitePublishWindowSource`)
    and returns its eligibility result for ``channel_id`` — the
    roadmap gate (≥10 published videos AND ≥28 days of analytics)
    is enforced by the service itself; a channel below either
    threshold gets ``eligible=False`` / ``reason='not_enough_data'``
    regardless of any learned ``publish_window`` rules it may hold.

    Read-only by construction: no Learning Store is supplied, so a
    call never writes a learning rule, and (as everywhere in this
    package) no schedule row is ever created or modified — an
    eligible suggestion is returned as data only, with the
    service's ``learning_rule_note`` saying exactly that.

    ``conn_factory`` follows the codebase store convention (a
    callable returning a fresh connection, or a bare connection);
    the default is the app DB layer's ``get_connection``.
    """
    if conn_factory is None:
        from app import db as db_module

        conn_factory = db_module.get_connection
    service = PublishWindowService(SqlitePublishWindowSource(conn_factory))
    result = dict(service.suggest(str(channel_id)))
    # API-facing status/source labels (the learning service returns
    # the sibling structure verbatim; the Growth UI reads `status`).
    if result.get("eligible"):
        result["status"] = "eligible"
    elif result.get("reason") == "channel_not_found":
        result["status"] = "channel_not_found"
    else:
        result["status"] = "insufficient_data"
    result["source"] = "publish_window_service"
    return result
