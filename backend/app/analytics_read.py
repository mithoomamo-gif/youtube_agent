"""Analytics READ model (Round 5, Growth + Learning) — service layer.

This module is the read side of analytics ingestion: it turns the
snapshot tables written by the sync/storage siblings into the
owner-scoped shapes the UI / reporting teams consume.  It never
writes analytics data, never calls YouTube, and never invents a
number: every figure it returns carries the ``captured_at`` of the
snapshot it came from, and absence is reported as an explicit typed
state, never as a zero.

Storage contract consumed (all landed by sibling Round 5 workers)
------------------------------------------------------------------
* :class:`app.analytics.store.AnalyticsStore` — per-video snapshots
  in ``analytics_snapshots`` (kinds ``d3`` / ``d7`` / ``d28`` /
  ``d90`` / ``manual`` / ``scheduled``) plus
  ``last_synced_at(channel_id)`` derivation.  Per-video reads go
  through the store, so its owner scoping applies unchanged.
* ``channel_metric_snapshots`` — channel-level snapshots.  There
  is exactly ONE definition of this table in the tree: the storage
  team's canonical DDL in ``app.analytics.schema``
  (``subscribers`` / ``watch_hours`` / ``qualified_watch_hours`` /
  ``captured_at`` / ``capture_day`` / ``snapshot_kind``).  The sync
  layer carries NO competing DDL — it delegates to
  ``ensure_analytics_schema`` and writes through the store
  exclusively (R5-ANALYTICS-001 settled; R5-002 closure removed the
  last silent-swallow residue of the sync-side definition).  The
  channel reader below remains column-adaptive (PRAGMA-driven, the
  ``app.routers.channels`` pattern) ONLY to tolerate rows written
  during the brief pre-alignment window, whose shape used
  ``subscriber_count`` / ``watch_hours_x100`` (a *period* figure) /
  ``snapshot_at`` / ``snapshot_day``; fields such a legacy row
  cannot supply stay ``None`` — most importantly
  ``qualified_watch_hours``, which that shape does not carry at
  all, so YPP qualification then reports itself unavailable rather
  than estimating from ordinary watch time (MASTER_ROADMAP: "The
  app never estimates qualification from ordinary watch time if
  Analytics qualification data is unavailable").
  On canonical rows the sync records period figures in
  ``metrics_json`` under ``*_period`` keys (``views_period``,
  ``watch_hours_period`` / ``watch_minutes_period``,
  ``subs_delta_period``, ``subscribers_gained/lost_period``,
  ``likes_period``, ``comments_period``, ``shares_period``,
  ``avg_view_duration_sec_period``, ``period_start/end_date``);
  the normaliser builds the wire ``period`` block from those keys
  for canonical rows and from the legacy columns for pre-alignment
  rows.
* ``analytics_sync_state`` (``app.analytics_sync``) — documented by
  the sync team as "the freshness surface the read-API team
  consumes": one row per channel with ``last_synced_at`` (last
  *successful* sync), ``last_attempt_at``, ``last_status``,
  ``last_reason``, ``last_run_id``.  Read directly here (defensively:
  the table may not exist yet); when no row exists the status is
  derived honestly — ``never_synced`` when nothing was ever
  captured, ``unknown`` when snapshots exist but no sync state was
  recorded.

Honest states
-------------
Every response carries ``data_state``:

* ``not_connected`` — the channel has no live YouTube connection
  (no credential, or an explicit ``needs_reauth`` / ``not_connected``
  / disconnected state).  Metrics are ``None``; sync status and
  ``last_synced_at`` are still reported.
* ``no_data`` — connected, but nothing has been captured yet
  (``last_synced_at`` is ``None``), or — per video item — that video
  has no snapshots.
* ``ok`` — figures follow, each with its snapshot provenance.

A real ``0`` stored in a snapshot is data and is returned as ``0``
(with its ``captured_at``); only *absence* becomes ``None`` / a
typed state.

Owner scoping: channels resolve by internal id or YouTube channel
id (the codebase convention); an unknown channel raises 404
``channel_not_found``.  All snapshot reads are filtered by the
resolved internal channel id, and a video id that belongs to another
channel answers 404 on the per-video route — existence never leaks
across channels.
"""

from __future__ import annotations

import json
import statistics
from typing import Any, Optional

from app.errors import ApiError

#: YPP full-path thresholds (MASTER_ROADMAP: subscribers current/1,000
#: and qualified public watch hours current/4,000).
YPP_SUBSCRIBERS_REQUIRED = 1000
YPP_WATCH_HOURS_REQUIRED = 4000.0

#: Metric keys compared against the channel baseline, in wire order.
BASELINE_METRICS = (
    "views",
    "watch_hours",
    "likes",
    "comments_count",
    "shares",
    "ctr",
    "avg_view_duration_sec",
    "subs_gained",
)

#: How many of the channel's other videos form a baseline pool.
BASELINE_POOL_SIZE = 10
#: A baseline needs at least this many pool videos, else it is null
#: with reason ``insufficient_baseline`` — never a fake comparison.
BASELINE_MIN_VIDEOS = 2

_NOT_CONNECTED_STATES = {"needs_reauth", "not_connected", "connect_failed"}
_CONNECTED_STATES = {"connected", "needs_audit_check"}


# ---------------------------------------------------------------------------
# Connection / schema helpers (PRAGMA-adaptive, channels-router pattern)
# ---------------------------------------------------------------------------


def _conn():
    from app import db as db_module

    return db_module.get_connection()


def _store():
    from app.analytics.store import AnalyticsStore
    from app import db as db_module

    return AnalyticsStore(db_module.get_connection)


def _tables(conn) -> set:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _cols(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _parse_config(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        if isinstance(data, dict):
            return data
    return {}


def _parse_json(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        if isinstance(data, dict):
            return data
    return {}


# ---------------------------------------------------------------------------
# Channel resolution + connection state
# ---------------------------------------------------------------------------


def resolve_channel(conn, channel_ref: str) -> dict:
    """Resolve a channel by internal id OR YouTube channel id.

    Raises 404 ``channel_not_found`` for unknown / soft-deleted
    channels — every analytics read starts here, so cross-channel
    existence never leaks.
    """
    if "channels" not in _tables(conn):
        raise ApiError(
            404, "channel_not_found", f"No channel with id '{channel_ref}'."
        )
    cols = _cols(conn, "channels")
    where_deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
    row = conn.execute(
        "SELECT * FROM channels WHERE (id = ? OR youtube_channel_id = ?) "
        f"{where_deleted} LIMIT 1",
        (str(channel_ref), str(channel_ref)),
    ).fetchone()
    if row is None:
        raise ApiError(
            404, "channel_not_found", f"No channel with id '{channel_ref}'."
        )
    return dict(row)


def channel_is_connected(channel: dict) -> bool:
    """Whether the channel currently has a live YouTube connection.

    An explicit negative state (disconnected lifecycle, or
    ``needs_reauth`` / ``not_connected`` in the connect/credential
    state stored in ``config_json``) wins over any stale positive
    signal; otherwise a credential link or a positive connect state
    counts as connected.  No signal at all means not connected —
    analytics cannot exist for a channel that was never linked.
    """
    if str(channel.get("status") or "") == "disconnected":
        return False
    config = _parse_config(channel.get("config_json"))
    connect_status = config.get("connect_status")
    credential_status = config.get("credential_status")
    if connect_status in _NOT_CONNECTED_STATES:
        return False
    if credential_status in _NOT_CONNECTED_STATES:
        return False
    if channel.get("credential_id"):
        return True
    if connect_status in _CONNECTED_STATES:
        return True
    if credential_status == "connected":
        return True
    return False


# ---------------------------------------------------------------------------
# Freshness + sync status (reported on EVERY response)
# ---------------------------------------------------------------------------


def sync_status(conn, channel_pk: str, store_last_synced: Optional[int]) -> dict:
    """The channel's sync status block.

    Prefers the sync team's ``analytics_sync_state`` row (the
    documented freshness surface); falls back to an honest derived
    status when no row/table exists yet.
    """
    status: dict[str, Any] = {
        "status": None,
        "last_reason": None,
        "last_attempt_at": None,
        "last_run_id": None,
        "last_snapshots_written": 0,
        "source": "derived",
    }
    if "analytics_sync_state" in _tables(conn):
        row = conn.execute(
            "SELECT * FROM analytics_sync_state WHERE channel_id = ?",
            (channel_pk,),
        ).fetchone()
        if row is not None:
            data = dict(row)
            status.update(
                {
                    "status": data.get("last_status") or "never_synced",
                    "last_reason": data.get("last_reason"),
                    "last_attempt_at": data.get("last_attempt_at"),
                    "last_run_id": data.get("last_run_id"),
                    "last_snapshots_written": (
                        data.get("last_snapshots_written") or 0
                    ),
                    "source": "analytics_sync_state",
                }
            )
            return status
    status["status"] = (
        "never_synced" if store_last_synced is None else "unknown"
    )
    return status


def _derived_last_synced(conn, channel_pk: str) -> Optional[int]:
    """Max capture instant across the snapshot tables, computed
    column-adaptively.  Fallback for when the store's own derivation
    cannot run (its SQL names the canonical ``captured_at`` column,
    which pre-alignment legacy ``channel_metric_snapshots`` rows —
    written under the removed sync-side shape — do not have)."""
    values: list[int] = []
    tables = _tables(conn)
    if "analytics_snapshots" in tables:
        row = conn.execute(
            "SELECT MAX(snapshot_at) FROM analytics_snapshots "
            "WHERE channel_id = ?",
            (channel_pk,),
        ).fetchone()
        if row is not None and row[0] is not None:
            values.append(int(row[0]))
    if "channel_metric_snapshots" in tables:
        cols = _cols(conn, "channel_metric_snapshots")
        for col in ("captured_at", "snapshot_at"):
            if col in cols:
                row = conn.execute(
                    f"SELECT MAX({col}) FROM channel_metric_snapshots "
                    "WHERE channel_id = ?",
                    (channel_pk,),
                ).fetchone()
                if row is not None and row[0] is not None:
                    values.append(int(row[0]))
                break
    return max(values) if values else None


def freshness(conn, channel_pk: str) -> tuple[Optional[int], dict]:
    """``(last_synced_at, sync_status)`` for one channel.

    ``last_synced_at`` prefers the sync state's last-*successful*
    sync instant; when the state row has none (or does not exist),
    the store's derivation (max capture instant across both snapshot
    tables) is used.  ``None`` means never synced — never a fake 0.
    """
    store_value: Optional[int] = None
    try:
        store_value = _store().last_synced_at(channel_pk)
    except Exception:
        store_value = None
    if store_value is None:
        try:
            store_value = _derived_last_synced(conn, channel_pk)
        except Exception:
            store_value = None
    status = sync_status(conn, channel_pk, store_value)
    last_synced = store_value
    if status["source"] == "analytics_sync_state":
        row = conn.execute(
            "SELECT last_synced_at FROM analytics_sync_state "
            "WHERE channel_id = ?",
            (channel_pk,),
        ).fetchone()
        if row is not None and row[0] is not None:
            last_synced = int(row[0])
    return last_synced, status


def _envelope(
    channel: dict,
    data_state: str,
    last_synced_at: Optional[int],
    status: dict,
) -> dict:
    return {
        "channel_id": str(channel["id"]),
        "youtube_channel_id": channel.get("youtube_channel_id"),
        "data_state": data_state,
        "last_synced_at": last_synced_at,
        "sync": status,
    }


# ---------------------------------------------------------------------------
# Channel metric snapshots (canonical shape + legacy-row tolerance
# — see module docstring)
# ---------------------------------------------------------------------------


def _first_present(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _normalise_channel_row(row: dict, cols: set) -> dict:
    """Normalise one ``channel_metric_snapshots`` row (canonical
    shape, or a pre-alignment legacy row) into the single wire
    shape."""
    extra = _parse_json(row.get("metrics_json"))
    captured = _first_present(row.get("captured_at"), row.get("snapshot_at"))
    subscribers = _first_present(
        row.get("subscribers"),
        row.get("subscriber_count"),
        extra.get("subscribers"),
        extra.get("subscriber_count"),
    )
    total_views = _first_present(row.get("total_views"), extra.get("total_views"))
    total_videos = _first_present(
        row.get("total_videos"), extra.get("total_videos"), extra.get("video_count")
    )
    # Ordinary watch hours: the storage shape stores them directly
    # (REAL hours).  The sync shape's watch_hours_x100 is a PERIOD
    # figure — it is reported under ``period`` below, never relabelled
    # as the channel's ordinary/lifetime hours.
    ordinary_watch_hours = _first_present(
        row.get("watch_hours"), extra.get("watch_hours")
    )
    # Qualified watch hours: ONLY the Analytics qualification figure.
    # Absent column / NULL value means unavailable — never estimated
    # from ordinary watch time (roadmap hard rule).
    qualified = _first_present(
        row.get("qualified_watch_hours"),
        extra.get("qualified_watch_hours"),
        extra.get("qualified_public_watch_hours"),
    )
    period = None
    if "views_period" in cols or "watch_hours_x100" in cols:
        # Legacy sync shape (pre-R5-ANALYTICS-001 alignment): period
        # figures lived in dedicated columns.
        period = {
            "views": row.get("views_period"),
            "watch_hours": (
                float(row["watch_hours_x100"]) / 100.0
                if row.get("watch_hours_x100") is not None
                else None
            ),
            "subs_delta": row.get("subs_delta_period"),
            "likes": row.get("likes_period"),
            "comments_count": row.get("comments_period"),
            "shares": row.get("shares_period"),
        }
        if all(value is None for value in period.values()):
            period = None
    if period is None and extra:
        # Settled storage shape: the sync writes the period figures
        # into ``metrics_json`` under ``*_period`` keys (the value
        # columns carry lifetime figures; ``watch_hours`` there is
        # the ordinary period-hours figure and stays reported as
        # ``ordinary_watch_hours`` above — the period block below is
        # the explicitly-labelled period view of the same sync).
        watch_hours_period = extra.get("watch_hours_period")
        if watch_hours_period is None and (
            extra.get("watch_minutes_period") is not None
        ):
            watch_hours_period = float(extra["watch_minutes_period"]) / 60.0
        candidate = {
            "views": extra.get("views_period"),
            "watch_hours": watch_hours_period,
            "subs_delta": extra.get("subs_delta_period"),
            "subscribers_gained": extra.get("subscribers_gained_period"),
            "subscribers_lost": extra.get("subscribers_lost_period"),
            "likes": extra.get("likes_period"),
            "comments_count": extra.get("comments_period"),
            "shares": extra.get("shares_period"),
            "avg_view_duration_sec": extra.get(
                "avg_view_duration_sec_period"
            ),
            "period_start_date": extra.get("period_start_date"),
            "period_end_date": extra.get("period_end_date"),
        }
        if any(value is not None for value in candidate.values()):
            period = candidate
    return {
        "subscriber_count": subscribers,
        "total_views": total_views,
        "total_videos": total_videos,
        "ordinary_watch_hours": (
            float(ordinary_watch_hours) if ordinary_watch_hours is not None else None
        ),
        "qualified_watch_hours": (
            float(qualified) if qualified is not None else None
        ),
        "period": period,
        "snapshot_kind": row.get("snapshot_kind") or "scheduled",
        "captured_at": int(captured) if captured is not None else None,
        "capture_day": row.get("capture_day") or row.get("snapshot_day"),
        "derived_from_snapshot": True,
    }


def latest_channel_metrics(conn, channel_pk: str) -> Optional[dict]:
    """The channel's most recent channel-metric snapshot, normalised,
    or ``None`` when none was ever captured."""
    if "channel_metric_snapshots" not in _tables(conn):
        return None
    cols = _cols(conn, "channel_metric_snapshots")
    rows = conn.execute(
        "SELECT * FROM channel_metric_snapshots WHERE channel_id = ?",
        (channel_pk,),
    ).fetchall()
    normalised = [
        _normalise_channel_row(dict(row), cols) for row in rows
    ]
    normalised = [n for n in normalised if n["captured_at"] is not None]
    if not normalised:
        return None
    return max(normalised, key=lambda n: n["captured_at"])


def _progress(current: Optional[float], required: float) -> dict:
    """One YPP progress leg.  ``current=None`` stays None end to end —
    remaining / met / pct are never computed from an invented base."""
    if current is None:
        return {
            "current": None,
            "required": required,
            "remaining": None,
            "met": None,
            "pct": None,
        }
    remaining = max(required - float(current), 0.0)
    return {
        "current": current,
        "required": required,
        "remaining": remaining,
        "met": float(current) >= required,
        "pct": round(min(float(current) / required * 100.0, 100.0), 2)
        if required
        else None,
    }


def ypp_progress(metrics: Optional[dict]) -> Optional[dict]:
    """YPP progress derived from one channel-metric snapshot.

    Subscribers progress uses the snapshot's subscriber count;
    watch-hours progress uses ONLY the snapshot's qualified public
    watch hours.  When the snapshot carries no qualification figure,
    ``qualification_data_available`` is False and the watch-hours leg
    is all-``None`` — ordinary watch hours are reported alongside,
    clearly labelled, and never substituted.
    """
    if metrics is None:
        return None
    qualified = metrics.get("qualified_watch_hours")
    watch_leg = _progress(qualified, YPP_WATCH_HOURS_REQUIRED)
    watch_leg["current_qualified"] = watch_leg.pop("current")
    return {
        "derived_from_snapshot": True,
        "captured_at": metrics.get("captured_at"),
        "subscribers": _progress(
            metrics.get("subscriber_count"), YPP_SUBSCRIBERS_REQUIRED
        ),
        "watch_hours": {
            **watch_leg,
            "qualification_data_available": qualified is not None,
            "ordinary_watch_hours": metrics.get("ordinary_watch_hours"),
            "note": (
                "Watch-hours progress uses qualified public watch "
                "hours from the snapshot only; ordinary watch time "
                "is never estimated into qualification."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Per-video metrics normalisation + baseline math
# ---------------------------------------------------------------------------


def normalise_video_metrics(snapshot: dict) -> dict:
    """One video snapshot's metrics in the single wire shape.

    Values come from the store's parsed ``metrics`` mapping first,
    with the mirrored legacy columns as fallback (legacy rows whose
    metrics were synthesised by the store already carry both).
    Missing values stay ``None``.
    """
    metrics = snapshot.get("metrics") or {}
    watch_hours = metrics.get("watch_hours")
    if watch_hours is None and metrics.get("watch_minutes") is not None:
        watch_hours = float(metrics["watch_minutes"]) / 60.0
    if watch_hours is None and snapshot.get("watch_hours_x100") is not None:
        watch_hours = float(snapshot["watch_hours_x100"]) / 100.0
    ctr = metrics.get("ctr")
    if ctr is None and metrics.get("ctr_bps") is not None:
        ctr = float(metrics["ctr_bps"]) / 10_000.0
    if ctr is None and snapshot.get("ctr_bps") is not None:
        ctr = float(snapshot["ctr_bps"]) / 10_000.0
    comments = _first_present(
        metrics.get("comments_count"),
        metrics.get("comments"),
        snapshot.get("comments_count"),
    )
    subs = _first_present(
        metrics.get("subs_gained"),
        metrics.get("subs_delta"),
        snapshot.get("subs_delta"),
    )
    return {
        "views": _first_present(metrics.get("views"), snapshot.get("views")),
        "watch_hours": watch_hours,
        "likes": _first_present(metrics.get("likes"), snapshot.get("likes")),
        "comments_count": comments,
        "shares": _first_present(metrics.get("shares"), snapshot.get("shares")),
        "ctr": ctr,
        "avg_view_duration_sec": _first_present(
            metrics.get("avg_view_duration_sec"),
            snapshot.get("avg_view_duration_sec"),
        ),
        "avg_view_percentage": metrics.get("avg_view_percentage"),
        "subs_gained": subs,
        "revenue_micros": _first_present(
            metrics.get("revenue_micros"), snapshot.get("revenue_micros")
        ),
    }


def _snapshot_view(snapshot: dict) -> dict:
    return {
        "id": snapshot.get("id"),
        "snapshot_kind": snapshot.get("snapshot_kind"),
        "captured_at": snapshot.get("captured_at"),
        "capture_day": snapshot.get("capture_day"),
        "source": snapshot.get("source"),
        "project_id": snapshot.get("project_id"),
        "metrics": normalise_video_metrics(snapshot),
    }


def _known_videos(conn, channel_pk: str) -> list[dict]:
    """The channel's videos (publish records + snapshot-bearing ids),
    newest first: published_at desc (unpublished last), then latest
    capture desc, then video id for determinism."""
    videos: dict[str, dict] = {}
    if "publish_records" in _tables(conn) and "projects" in _tables(conn):
        rows = conn.execute(
            "SELECT pr.youtube_video_id AS vid, pr.project_id AS pid, "
            "pr.title AS title, pr.published_at AS published_at "
            "FROM publish_records pr "
            "JOIN projects p ON p.id = pr.project_id "
            "WHERE p.channel_id = ?",
            (channel_pk,),
        ).fetchall()
        for row in rows:
            data = dict(row)
            videos[str(data["vid"])] = {
                "youtube_video_id": str(data["vid"]),
                "project_id": data.get("pid"),
                "title": data.get("title"),
                "published_at": data.get("published_at"),
            }
    store = _store()
    try:
        snapshots = store.list_snapshots(channel_pk)
    except Exception:
        snapshots = []
    latest_capture: dict[str, int] = {}
    for snap in snapshots:
        vid = snap.get("youtube_video_id")
        if vid is None:
            continue
        vid = str(vid)
        captured = snap.get("captured_at") or 0
        latest_capture[vid] = max(latest_capture.get(vid, 0), int(captured))
        if vid not in videos:
            videos[vid] = {
                "youtube_video_id": vid,
                "project_id": snap.get("project_id"),
                "title": None,
                "published_at": None,
            }
        elif videos[vid].get("project_id") is None:
            videos[vid]["project_id"] = snap.get("project_id")
    def _sort_key(v: dict):
        published = v.get("published_at")
        return (
            0 if published is not None else 1,  # published first
            -(published or 0),  # newest publication first
            -latest_capture.get(v["youtube_video_id"], 0),
            v["youtube_video_id"],
        )

    return sorted(videos.values(), key=_sort_key)


def _comparison(current: dict, pool: list[dict]) -> Optional[dict]:
    """Median-baseline comparison for one video.

    ``pool`` is the other videos' latest snapshots OF THE SAME KIND.
    Returns ``None`` when the pool is too small — the caller reports
    ``insufficient_baseline`` instead of a fabricated comparison.
    """
    if len(pool) < BASELINE_MIN_VIDEOS:
        return None
    baseline: dict[str, Any] = {}
    for key in BASELINE_METRICS:
        values = [
            entry["metrics"].get(key)
            for entry in pool
            if entry["metrics"].get(key) is not None
        ]
        baseline[key] = statistics.median(values) if values else None
    deltas: dict[str, Any] = {}
    for key in BASELINE_METRICS:
        value = current["metrics"].get(key)
        base = baseline.get(key)
        if value is None or base is None:
            deltas[key] = {"value": value, "baseline": base,
                           "absolute": None, "pct": None}
            continue
        absolute = value - base
        pct = round(absolute / base * 100.0, 2) if base != 0 else None
        deltas[key] = {
            "value": value,
            "baseline": base,
            "absolute": absolute,
            "pct": pct,
        }
    return {
        "baseline_kind": current["snapshot_kind"],
        "baseline_video_count": len(pool),
        "baseline_median": baseline,
        "deltas": deltas,
    }


# ---------------------------------------------------------------------------
# Endpoint services
# ---------------------------------------------------------------------------


def channel_overview(channel_ref: str) -> dict:
    """Per-channel overview: latest channel metrics + YPP progress."""
    conn = _conn()
    try:
        channel = resolve_channel(conn, channel_ref)
        channel_pk = str(channel["id"])
        last_synced, status = freshness(conn, channel_pk)
        if not channel_is_connected(channel):
            return _envelope(channel, "not_connected", last_synced, status) | {
                "metrics": None,
                "ypp_progress": None,
            }
        metrics = latest_channel_metrics(conn, channel_pk)
        if metrics is None:
            return _envelope(channel, "no_data", last_synced, status) | {
                "metrics": None,
                "ypp_progress": None,
            }
        return _envelope(channel, "ok", last_synced, status) | {
            "metrics": metrics,
            "ypp_progress": ypp_progress(metrics),
        }
    finally:
        conn.close()


def channel_ypp(channel_ref: str) -> dict:
    """Flat YPP shape (the UI growth worker's documented contract:
    ``subscriber_count`` / ``qualified_watch_hours`` /
    ``qualification_data_available`` / ``ordinary_watch_hours`` /
    ``as_of``), backed by the same overview computation."""
    overview = channel_overview(channel_ref)
    metrics = overview.get("metrics") or {}
    progress = overview.get("ypp_progress")
    return overview | {
        "subscriber_count": metrics.get("subscriber_count"),
        "qualified_watch_hours": metrics.get("qualified_watch_hours"),
        "qualification_data_available": (
            metrics.get("qualified_watch_hours") is not None
        ),
        "ordinary_watch_hours": metrics.get("ordinary_watch_hours"),
        "as_of": metrics.get("captured_at"),
        "ypp_progress": progress,
    }


def channel_videos(channel_ref: str) -> dict:
    """Per-video performance with median-baseline comparisons."""
    conn = _conn()
    try:
        channel = resolve_channel(conn, channel_ref)
        channel_pk = str(channel["id"])
        last_synced, status = freshness(conn, channel_pk)
        base = _envelope(channel, "ok", last_synced, status)
        if not channel_is_connected(channel):
            return base | {
                "data_state": "not_connected",
                "items": [],
                "next_cursor": None,
            }
        videos = _known_videos(conn, channel_pk)
        if not videos:
            return base | {
                "data_state": "no_data",
                "items": [],
                "next_cursor": None,
            }
        store = _store()
        # Latest snapshot (any kind) per video + same-kind pools.
        latest_by_video: dict[str, Optional[dict]] = {}
        by_kind_latest: dict[str, dict[str, dict]] = {}
        for video in videos:
            vid = video["youtube_video_id"]
            rows = store.list_snapshots(channel_pk, video_external_id=vid)
            latest_by_video[vid] = rows[-1] if rows else None
            kinds: dict[str, dict] = {}
            for row in rows:
                kinds[str(row.get("snapshot_kind"))] = row
            by_kind_latest[vid] = kinds

        items: list[dict] = []
        for video in videos:
            vid = video["youtube_video_id"]
            latest = latest_by_video[vid]
            item: dict[str, Any] = dict(video)
            if latest is None:
                item.update(
                    {
                        "data_state": "no_data",
                        "latest_snapshot": None,
                        "comparison": None,
                        "comparison_unavailable_reason": "no_data",
                    }
                )
                items.append(item)
                continue
            current = _snapshot_view(latest)
            kind = str(latest.get("snapshot_kind"))
            pool: list[dict] = []
            for other in videos:
                other_id = other["youtube_video_id"]
                if other_id == vid:
                    continue
                snap = by_kind_latest[other_id].get(kind)
                if snap is not None:
                    pool.append(_snapshot_view(snap))
                if len(pool) >= BASELINE_POOL_SIZE:
                    break
            comparison = _comparison(current, pool)
            item.update(
                {
                    "data_state": "ok",
                    "latest_snapshot": current,
                    "comparison": comparison,
                    "comparison_unavailable_reason": (
                        None if comparison is not None
                        else "insufficient_baseline"
                    ),
                }
            )
            items.append(item)
        return base | {"items": items, "next_cursor": None}
    finally:
        conn.close()


def video_snapshots(
    channel_ref: str, video_id: str, *, kind: Optional[str] = None
) -> dict:
    """All snapshots for one video, grouped by kind (growth-over-age).

    A video unknown to THIS channel (including one owned by another
    channel) answers 404 ``video_not_found`` — never data.
    """
    from app.analytics.schema import SNAPSHOT_KINDS

    if kind is not None and kind not in SNAPSHOT_KINDS:
        raise ApiError(
            422,
            "invalid_snapshot_kind",
            f"snapshot_kind must be one of {list(SNAPSHOT_KINDS)}, "
            f"got '{kind}'.",
            details={"allowed": list(SNAPSHOT_KINDS)},
        )
    conn = _conn()
    try:
        channel = resolve_channel(conn, channel_ref)
        channel_pk = str(channel["id"])
        last_synced, status = freshness(conn, channel_pk)
        known = {
            v["youtube_video_id"] for v in _known_videos(conn, channel_pk)
        }
        if str(video_id) not in known:
            raise ApiError(
                404,
                "video_not_found",
                f"No video with id '{video_id}' on this channel.",
            )
        base = _envelope(channel, "ok", last_synced, status) | {
            "youtube_video_id": str(video_id),
        }
        if not channel_is_connected(channel):
            return base | {
                "data_state": "not_connected",
                "items": [],
                "by_kind": {},
                "next_cursor": None,
            }
        rows = _store().list_snapshots(
            channel_pk,
            video_external_id=str(video_id),
            snapshot_kind=kind,
        )
        views = [_snapshot_view(row) for row in rows]
        views.sort(key=lambda v: -(v["captured_at"] or 0))
        by_kind: dict[str, list[dict]] = {}
        for view in views:
            by_kind.setdefault(str(view["snapshot_kind"]), []).append(view)
        return base | {
            "data_state": "ok" if views else "no_data",
            "items": views,
            "by_kind": by_kind,
            "next_cursor": None,
        }
    finally:
        conn.close()


def video_retention(
    channel_ref: str, video_id: str, *, kind: Optional[str] = None
) -> dict:
    """Retention read for one video (Round 5 closure — the read route
    the Analytics UI documented but which had not landed).

    Shape: the usual envelope (``channel_id`` / ``data_state`` /
    ``last_synced_at`` / ``sync``) plus the retention fields both at
    top level and nested under ``retention`` (the UI's
    ``parseRetentionWire`` reads the nested-or-top-level record):
    ``status`` (``ok`` / ``unmapped`` / ``no_data`` — the
    :mod:`app.retention_sections` honest states), ``points`` (the
    stored curve, elapsed/watch ratios exactly as stored),
    ``sections``, ``steepest_drop``, ``annotations``,
    ``duration_s``, ``snapshot_kind`` and ``captured_at``.

    Honesty rules, mirroring the rest of this module:

    * a video unknown to THIS channel (including another channel's
      video) answers 404 ``video_not_found``; an unknown channel
      answers 404 ``channel_not_found`` — existence never leaks;
    * a not-connected channel reports ``data_state='not_connected'``
      with an empty ``no_data`` retention block, never a curve;
    * the curve comes from the video's most recently captured
      snapshot that actually carries one (older snapshots are not
      silently substituted for a newer curve-less capture's
      metadata: ``snapshot_kind`` / ``captured_at`` always name the
      snapshot the curve came from);
    * section mapping runs through
      :func:`app.retention_sections.map_snapshot_retention` ONLY
      when a real duration is recorded (an explicit
      ``duration_s`` / ``duration_seconds`` / ``video_duration_s``
      in the snapshot's metrics, or a persisted retention analysis
      for that snapshot).  Analytics storage records no duration
      today, so the usual outcome is ``unmapped``: the stored
      points are returned in ratio form with NO section labels and
      NO fabricated seconds — exactly the UI's documented fallback
      semantics for "curve present, no section map".
    """
    from app.analytics.schema import SNAPSHOT_KINDS

    if kind is not None and kind not in SNAPSHOT_KINDS:
        raise ApiError(
            422,
            "invalid_snapshot_kind",
            f"snapshot_kind must be one of {list(SNAPSHOT_KINDS)}, "
            f"got '{kind}'.",
            details={"allowed": list(SNAPSHOT_KINDS)},
        )

    def _empty_retention(status: str, annotations: list[str]) -> dict:
        return {
            "status": status,
            "points": [],
            "retention_curve": [],
            "sections": [],
            "steepest_drop": None,
            "overall": None,
            "annotations": annotations,
            "duration_s": None,
            "snapshot_kind": None,
            "captured_at": None,
            "timing_source": None,
            "timing_precision": None,
        }

    conn = _conn()
    try:
        channel = resolve_channel(conn, channel_ref)
        channel_pk = str(channel["id"])
        last_synced, status_block = freshness(conn, channel_pk)
        known = {
            v["youtube_video_id"]: v
            for v in _known_videos(conn, channel_pk)
        }
        if str(video_id) not in known:
            raise ApiError(
                404,
                "video_not_found",
                f"No video with id '{video_id}' on this channel.",
            )
        video = known[str(video_id)]
        base = _envelope(channel, "ok", last_synced, status_block) | {
            "youtube_video_id": str(video_id),
            "project_id": video.get("project_id"),
        }
        if not channel_is_connected(channel):
            retention = _empty_retention(
                "no_data",
                ["Channel is not connected — no retention data."],
            )
            return base | {
                "data_state": "not_connected",
                "retention": retention,
            } | retention

        rows = _store().list_snapshots(
            channel_pk,
            video_external_id=str(video_id),
            snapshot_kind=kind,
        )
        # Most recently captured first (list_snapshots is oldest-first).
        rows = sorted(
            rows, key=lambda r: -(r.get("captured_at") or 0)
        )

        def _curve_of(row: dict) -> list:
            curve = row.get("retention_curve")
            if curve is None:
                curve = row.get("retention_curve_json")
            if isinstance(curve, str):
                try:
                    import json as _json

                    curve = _json.loads(curve) if curve.strip() else None
                except Exception:
                    curve = None
            return curve if isinstance(curve, list) and curve else []

        chosen = next((r for r in rows if _curve_of(r)), None)
        if chosen is None:
            retention = _empty_retention(
                "no_data",
                [
                    "No retention curve available for this video — "
                    "nothing to map."
                ],
            )
            if rows:
                retention["snapshot_kind"] = rows[0].get("snapshot_kind")
                retention["captured_at"] = rows[0].get("captured_at")
            return base | {
                "data_state": "no_data",
                "retention": retention,
            } | retention

        curve = _curve_of(chosen)
        from app.retention_sections import parse_retention_curve

        try:
            points = [
                {
                    "elapsed_ratio": p.elapsed_ratio,
                    "watch_ratio": p.watch_ratio,
                }
                for p in parse_retention_curve(curve)
            ]
        except Exception:
            points = []
        if not points:
            retention = _empty_retention(
                "no_data",
                [
                    "The stored retention curve could not be parsed — "
                    "nothing to map."
                ],
            )
            retention["snapshot_kind"] = chosen.get("snapshot_kind")
            retention["captured_at"] = chosen.get("captured_at")
            return base | {
                "data_state": "no_data",
                "retention": retention,
            } | retention

        # A real duration, when one is actually recorded: a persisted
        # retention analysis for this snapshot wins (it already ran
        # the mapping with its own duration/section map); otherwise an
        # explicit duration key in the snapshot's metrics.
        duration_s: Optional[float] = None
        persisted = None
        try:
            from app.retention_sections import load_retention_analysis

            if chosen.get("id"):
                persisted = load_retention_analysis(str(chosen["id"]))
        except Exception:
            persisted = None
        if persisted is not None and (
            persisted.video_id in (None, str(video_id))
        ):
            mapped = persisted.model_dump(mode="json")
            retention = {
                "status": mapped.get("status"),
                "points": points,
                "retention_curve": points,
                "sections": mapped.get("sections") or [],
                "steepest_drop": mapped.get("steepest_drop"),
                "overall": mapped.get("overall"),
                "annotations": mapped.get("annotations") or [],
                "duration_s": mapped.get("duration_s"),
                "snapshot_kind": chosen.get("snapshot_kind"),
                "captured_at": chosen.get("captured_at"),
                "timing_source": mapped.get("timing_source"),
                "timing_precision": mapped.get("timing_precision"),
            }
            return base | {
                "data_state": "ok",
                "retention": retention,
            } | retention

        metrics = chosen.get("metrics") or {}
        for key in ("duration_s", "duration_seconds", "video_duration_s"):
            value = metrics.get(key)
            if value is not None:
                try:
                    if float(value) > 0:
                        duration_s = float(value)
                except (TypeError, ValueError):
                    duration_s = None
                if duration_s is not None:
                    break

        if duration_s is not None:
            from app.retention_sections import map_snapshot_retention

            mapped = map_snapshot_retention(
                chosen, duration_s, None
            ).model_dump(mode="json")
            retention = {
                "status": mapped.get("status"),
                "points": points,
                "retention_curve": points,
                "sections": mapped.get("sections") or [],
                "steepest_drop": mapped.get("steepest_drop"),
                "overall": mapped.get("overall"),
                "annotations": mapped.get("annotations") or [],
                "duration_s": mapped.get("duration_s"),
                "snapshot_kind": chosen.get("snapshot_kind"),
                "captured_at": chosen.get("captured_at"),
                "timing_source": mapped.get("timing_source"),
                "timing_precision": mapped.get("timing_precision"),
            }
            return base | {
                "data_state": "ok",
                "retention": retention,
            } | retention

        # Curve present, no recorded duration / section map: report
        # the points in ratio form, unmapped — never invent seconds
        # or section labels.
        retention = {
            "status": "unmapped",
            "points": points,
            "retention_curve": points,
            "sections": [],
            "steepest_drop": None,
            "overall": None,
            "annotations": [
                "Retention curve is stored, but no video duration "
                "or section map is recorded in analytics storage, "
                "so the curve is reported in elapsed/watch ratios "
                "only — no section labels or timestamps are "
                "fabricated (status: unmapped)."
            ],
            "duration_s": None,
            "snapshot_kind": chosen.get("snapshot_kind"),
            "captured_at": chosen.get("captured_at"),
            "timing_source": None,
            "timing_precision": None,
        }
        return base | {
            "data_state": "ok",
            "retention": retention,
        } | retention
    finally:
        conn.close()
