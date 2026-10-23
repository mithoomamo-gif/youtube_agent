"""Analytics read seam for publish-window suggestions.

SEAM STATUS (honest)
=====================
* **Analytics read model: LANDED (this adapter, real).**  The
  sibling analytics-ingestion package (:mod:`app.analytics`,
  migration ``0007_analytics``) stores into the same
  ``analytics_snapshots`` table and mirrors the well-known metrics
  into the legacy discrete columns (``snapshot_at``, ``views``) on
  every write, so this adapter reads exactly the data that store
  writes: ``publish_records`` ⨝ ``projects`` (the channel's
  published videos and their publish instants) and
  ``analytics_snapshots`` (cumulative per-video metrics over
  time).  If a dedicated analytics *read-model* API lands later,
  it only has to satisfy :class:`PublishWindowSource`; the
  service does not change.
* **First-24h velocity derivation.** ``analytics_snapshots.views``
  is cumulative, so a video's first-24h velocity is derived, not
  stored: ``views`` at the latest snapshot in
  ``(published_at, published_at + 24h]`` minus ``views`` at the
  latest snapshot at or before ``published_at`` (baseline, normally
  none / 0).  A video with **no** snapshot inside its first 24h
  gets ``first_24h_velocity = None``: it still counts toward the
  published-video total, but contributes no velocity evidence —
  the service reports it under ``videos_without_velocity`` rather
  than guessing a number.
* **Channel timezone.**  There is no timezone column on
  ``channels`` anywhere in the schema.  The stored channel
  timezone is therefore read from ``channels.config_json`` key
  ``"timezone"`` (an IANA name, e.g. ``"Asia/Karachi"``).  When the
  key is absent or not a valid IANA name the adapter falls back to
  ``"UTC"`` and says so via ``timezone_source='fallback_utc'`` —
  the fallback is surfaced in every result, never silent.
* **Synthetic fixtures.** :class:`InMemoryPublishWindowSource`
  serves hand-built :class:`ChannelPublishData` (tests label their
  records synthetic by construction); it performs no I/O.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "ChannelPublishData",
    "InMemoryPublishWindowSource",
    "PublishedVideoVelocity",
    "PublishWindowSource",
    "SqlitePublishWindowSource",
    "VELOCITY_METRIC",
]

#: The velocity metric every record in this seam carries.
VELOCITY_METRIC = "views_first_24h"

_DAY_MS = 86_400_000
_FIRST_24H_MS = _DAY_MS


@dataclass(frozen=True)
class PublishedVideoVelocity:
    """One published video and its derived first-24h velocity.

    ``first_24h_velocity`` is ``None`` when the analytics source
    holds no snapshot inside the video's first 24 hours (see the
    module docstring) — unknown, never zero-filled.
    """

    video_id: str  # YouTube video id
    project_id: Optional[str]
    published_at_ms: int
    first_24h_velocity: Optional[float]
    velocity_metric: str = VELOCITY_METRIC


@dataclass(frozen=True)
class ChannelPublishData:
    """Everything the service needs for one channel, and only it.

    ``analytics_first_ms`` / ``analytics_last_ms`` bound the
    channel's analytics data span (min/max ``snapshot_at`` over
    the channel's snapshots); both are ``None`` when the channel
    has no snapshots at all.
    """

    channel_id: str
    exists: bool
    timezone_name: str = "UTC"
    timezone_source: str = "fallback_utc"  # or "channel_config"
    niche: Optional[str] = None
    videos: tuple[PublishedVideoVelocity, ...] = ()
    analytics_first_ms: Optional[int] = None
    analytics_last_ms: Optional[int] = None


class PublishWindowSource(Protocol):
    """Read-only source of per-channel publish/analytics data."""

    def fetch_channel(self, channel_id: str) -> ChannelPublishData:
        ...


# ---------------------------------------------------------------------------
# In-memory source (synthetic fixtures / tests)
# ---------------------------------------------------------------------------


class InMemoryPublishWindowSource:
    """Serves pre-built :class:`ChannelPublishData` from a mapping.

    Unknown channel ids return ``exists=False`` with no videos —
    the same honest shape the SQLite adapter returns for a missing
    channel.
    """

    def __init__(self, channels: Mapping[str, ChannelPublishData]) -> None:
        self._channels = dict(channels)

    def fetch_channel(self, channel_id: str) -> ChannelPublishData:
        data = self._channels.get(str(channel_id))
        if data is None:
            return ChannelPublishData(channel_id=str(channel_id), exists=False)
        return data


# ---------------------------------------------------------------------------
# Real SQLite adapter
# ---------------------------------------------------------------------------


def _parse_channel_timezone(config_raw: Any) -> tuple[str, str]:
    """(timezone_name, timezone_source) from a channels.config_json."""
    config: Any = None
    if isinstance(config_raw, str) and config_raw.strip():
        try:
            config = json.loads(config_raw)
        except ValueError:
            config = None
    if isinstance(config, dict):
        candidate = config.get("timezone")
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip()
            try:
                ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                return "UTC", "fallback_utc"
            return name, "channel_config"
    return "UTC", "fallback_utc"


def _derive_first_24h_velocity(
    published_at_ms: int,
    snapshots: Sequence[tuple[int, int]],
) -> Optional[float]:
    """First-24h velocity from cumulative (snapshot_at, views) rows.

    ``snapshots`` must be sorted by ``snapshot_at`` ascending.
    Returns ``None`` when no snapshot falls inside the first 24h.
    """
    baseline = 0
    views_24h: Optional[int] = None
    horizon = published_at_ms + _FIRST_24H_MS
    for snapshot_at, views in snapshots:
        if snapshot_at <= published_at_ms:
            baseline = int(views)
        elif snapshot_at <= horizon:
            views_24h = int(views)
        else:
            break
    if views_24h is None:
        return None
    return float(max(0, views_24h - baseline))


class SqlitePublishWindowSource:
    """Real adapter over publish_records / projects / snapshots.

    Read-only: every statement is a SELECT.  Constructed with a
    ``conn_factory`` (callable returning a fresh connection) or a
    bare connection, following the codebase store convention
    (:class:`app.style_lab.store.StyleLabStore`).
    """

    def __init__(self, conn_factory: Any) -> None:
        if hasattr(conn_factory, "execute"):
            self._shared_conn: Optional[sqlite3.Connection] = conn_factory
            self._conn_factory = None
        elif callable(conn_factory):
            self._shared_conn = None
            self._conn_factory = conn_factory
        else:
            raise TypeError("conn_factory must be callable or a connection")

    def _connect(self) -> tuple[sqlite3.Connection, bool]:
        if self._shared_conn is not None:
            return self._shared_conn, False
        return self._conn_factory(), True  # type: ignore[misc]

    def fetch_channel(self, channel_id: str) -> ChannelPublishData:
        channel_id = str(channel_id)
        conn, should_close = self._connect()
        try:
            row = conn.execute(
                "SELECT config_json, default_niche_profile_id FROM channels"
                " WHERE id = ? AND deleted_at IS NULL",
                (channel_id,),
            ).fetchone()
            if row is None:
                return ChannelPublishData(channel_id=channel_id, exists=False)
            config_raw, niche_profile_id = row[0], row[1]
            timezone_name, timezone_source = _parse_channel_timezone(config_raw)

            niche: Optional[str] = None
            if niche_profile_id:
                niche_row = conn.execute(
                    "SELECT key FROM niche_profiles WHERE id = ?",
                    (niche_profile_id,),
                ).fetchone()
                niche = str(niche_row[0]) if niche_row else str(niche_profile_id)

            # The channel's published videos (publish_records is the
            # publish ledger; the channel comes via its project).
            published = conn.execute(
                "SELECT pr.youtube_video_id, pr.project_id,"
                " COALESCE(pr.published_at, p.published_at)"
                " FROM publish_records pr"
                " JOIN projects p ON p.id = pr.project_id"
                " WHERE p.channel_id = ?"
                " AND COALESCE(pr.published_at, p.published_at) IS NOT NULL"
                " ORDER BY 3 ASC",
                (channel_id,),
            ).fetchall()

            # All of the channel's snapshots, grouped per video.
            span_row = conn.execute(
                "SELECT MIN(snapshot_at), MAX(snapshot_at)"
                " FROM analytics_snapshots WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            analytics_first = span_row[0] if span_row else None
            analytics_last = span_row[1] if span_row else None

            snapshots_by_video: dict[str, list[tuple[int, int]]] = {}
            for yt_id, snapshot_at, views in conn.execute(
                "SELECT youtube_video_id, snapshot_at, views"
                " FROM analytics_snapshots WHERE channel_id = ?"
                " AND youtube_video_id IS NOT NULL"
                " ORDER BY youtube_video_id, snapshot_at ASC",
                (channel_id,),
            ):
                snapshots_by_video.setdefault(str(yt_id), []).append(
                    (int(snapshot_at), int(views or 0))
                )

            videos = tuple(
                PublishedVideoVelocity(
                    video_id=str(yt_id),
                    project_id=str(project_id) if project_id else None,
                    published_at_ms=int(published_at),
                    first_24h_velocity=_derive_first_24h_velocity(
                        int(published_at),
                        snapshots_by_video.get(str(yt_id), []),
                    ),
                )
                for yt_id, project_id, published_at in published
            )
            return ChannelPublishData(
                channel_id=channel_id,
                exists=True,
                timezone_name=timezone_name,
                timezone_source=timezone_source,
                niche=niche,
                videos=videos,
                analytics_first_ms=(
                    int(analytics_first) if analytics_first is not None else None
                ),
                analytics_last_ms=(
                    int(analytics_last) if analytics_last is not None else None
                ),
            )
        finally:
            if should_close:
                conn.close()
