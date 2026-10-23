"""Analytics read endpoints (Round 5, Growth + Learning).

Owner-scoped READ API over the analytics snapshot storage, nested
under the owning channel like every other router::

    GET /api/v1/channels/{channel_id}/analytics/overview
    GET /api/v1/channels/{channel_id}/analytics/videos
    GET /api/v1/channels/{channel_id}/analytics/videos/{video_id}/snapshots
    GET /api/v1/channels/{channel_id}/analytics/videos/{video_id}/retention
    GET /api/v1/channels/{channel_id}/ypp-progress

All logic lives in :mod:`app.analytics_read`; this module is the
thin HTTP surface (the ``app.routers.style_lab`` pattern).  The
``ApiError`` envelope from :mod:`app.errors` carries every failure,
and the launch-token middleware (mounted by ``app.main``) guards
everything.

Response conventions (see :mod:`app.analytics_read` for the full
contract):

* EVERY response carries ``last_synced_at`` (epoch ms, ``None``
  when the channel has never synced) and a ``sync`` status block
  (last result / reason from the sync team's
  ``analytics_sync_state`` surface when it exists).
* ``data_state`` is ``ok`` / ``no_data`` / ``not_connected`` —
  absence is a typed state, never a zero dressed as data.  A real
  ``0`` from a snapshot is returned as ``0`` with its
  ``captured_at`` provenance.
* Unknown channel -> 404 ``channel_not_found``; a video id that is
  not the channel's own (including another channel's video) -> 404
  ``video_not_found``.  Cross-channel existence never leaks.
* ``ypp-progress`` is the flat alias the UI growth worker documented
  (``subscriber_count`` / ``qualified_watch_hours`` /
  ``qualification_data_available`` / ``ordinary_watch_hours`` /
  ``as_of``) over the same overview computation; qualification is
  never estimated from ordinary watch time.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from app import analytics_read

router = APIRouter(prefix="/api/v1/channels", tags=["analytics"])


@router.get("/{channel_id}/analytics/overview")
async def analytics_overview(channel_id: str) -> dict:
    """Latest channel metrics + snapshot-derived YPP progress."""
    return analytics_read.channel_overview(channel_id)


@router.get("/{channel_id}/analytics/videos")
async def analytics_videos(channel_id: str) -> dict:
    """Per-video performance vs the channel's median baseline."""
    return analytics_read.channel_videos(channel_id)


@router.get("/{channel_id}/analytics/videos/{video_id}/snapshots")
async def analytics_video_snapshots(
    channel_id: str, video_id: str, kind: Optional[str] = None
) -> dict:
    """One video's snapshots across kinds (growth over age)."""
    return analytics_read.video_snapshots(channel_id, video_id, kind=kind)


@router.get("/{channel_id}/analytics/videos/{video_id}/retention")
async def analytics_video_retention(
    channel_id: str, video_id: str, kind: Optional[str] = None
) -> dict:
    """One video's retention curve (+ section mapping when a real
    duration/map is recorded; honest ``unmapped`` / ``no_data``
    states otherwise — see :func:`app.analytics_read.video_retention`)."""
    return analytics_read.video_retention(channel_id, video_id, kind=kind)


@router.get("/{channel_id}/ypp-progress")
async def ypp_progress(channel_id: str) -> dict:
    """Flat YPP progress shape (alias of the overview computation)."""
    return analytics_read.channel_ypp(channel_id)
