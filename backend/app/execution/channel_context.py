"""Channel context resolution + per-channel usage view (Round 4, W3).

One tested way for the execution / integration path to answer, for a
given project: *which channel does this belong to, and what are that
channel's effective profile, voice and budget parameters?*

Precedence rules (each grounded in existing code — see notes)
--------------------------------------------------------------
Niche profile:
    1. the project's **schedule's** ``niche_profile_id``
       (``projects.schedule_id`` -> ``schedules.niche_profile_id``),
    2. else the project's own ``niche_profile_id``,
    3. else the channel's ``default_niche_profile_id``.

    Rule 1 is the materialisation contract already in the codebase:
    :class:`app.execution.scheduler.SchedulerTick` copies the
    schedule's channel / niche / publish_mode onto every project it
    creates, so a scheduled project normally carries the same niche in
    slot 2 as well; resolving through the schedule first keeps the
    answer correct even if the project row is later edited.  A
    schedule whose ``channel_id`` does **not** match the project's
    channel is ignored entirely (niche *and* budget) — a mismatched
    schedule is cross-talk, never a source of truth.

Voice:
    ``niche_profiles.default_voice_id`` (with ``voice_style`` carried
    alongside as the hint the voice stage falls back to — see
    ``app.integration.voice_stage._resolve_voice``).

    There is deliberately **no** channel-level voice override: a
    codebase search finds no code that reads or writes a voice key in
    ``channels.config_json`` (that column today carries only the
    connect-state keys ``connect_status`` / ``credential_status`` /
    ``audit_status`` / ``testing_mode_token``, written by
    ``app.routers.channels``).  This module follows the code and does
    not invent an override convention.

Quality tier (Round 5):
    ``channels.quality_tier`` (``draft`` / ``master_1080p`` /
    ``master_1440p_upscale``, default ``draft``) — the channel's
    master-encode setting, resolved here from the channel row (see
    :mod:`app.channel_quality`) and carried on the context so the
    stage bridge can put it on the production spec for both manual
    and scheduled projects.

Budgets (controls, not YouTube quota):
    * monthly cap — ``channels.monthly_budget_micros``;
    * daily cost cap — the (channel-matching) schedule's
      ``schedules.daily_cost_cap_micros``;
    * daily upload cap — ``channels.daily_upload_cap``.

    These are this app's budget / scheduling controls.  YouTube API
    quota is **per Google Cloud project**, so all channels share the
    app-level quota buckets exposed in Round 3 (``app.quota_view``);
    channels do not get separate YouTube quota.  See
    ``docs/ROUND4_CHANNEL_SCOPING.md``.

Per-channel usage view
----------------------
:func:`build_channel_usage_views` / :func:`build_channel_usage_view`
build the read-only view served by ``GET /api/v1/usage/channels``
(``app.routers.usage``).  Month-to-date spend is attributed strictly
through ``project_id -> projects.channel_id``:

* ``month_to_date_spend_micros`` — ``SUM(agent_runs.cost_micros)`` for
  runs in the current UTC calendar month whose project belongs to the
  channel (the execution layer's cost ledger; stage handlers accrue
  exactly these figures into ``projects.total_cost_micros``);
* ``month_to_date_api_usage_micros`` — the same window over the
  ``api_usage`` provider ledger, reported **separately** (never added
  to the figure above, so provider calls are never double-counted);
* ``projects_total_cost_micros`` — lifetime ``SUM(projects
  .total_cost_micros)`` for the channel.

Missing tables / no data degrade to honest zeros, matching the
existing usage-router convention.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

__all__ = [
    "ChannelContext",
    "parse_channel_config",
    "resolve_channel_context",
    "resolve_channel_context_from_conn",
    "build_channel_usage_view",
    "build_channel_usage_views",
    "month_window_ms",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def parse_channel_config(raw: Any) -> dict:
    """Tolerantly decode a ``channels.config_json`` value into a dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception:
            return {}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quality_tier_of(channel: Optional[dict]) -> str:
    """The channel row's quality tier (``draft`` when unset/unknown).

    Delegates to :mod:`app.channel_quality` (the column's owner);
    a missing column on a pre-migration database simply reads as
    the default.  Imported lazily to keep this module's import graph
    exactly as it was.
    """
    try:
        from app.channel_quality import quality_tier_of_row

        return quality_tier_of_row(channel)
    except Exception:
        return "draft"


def _row_dict(row: Any) -> Optional[dict]:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def month_window_ms(now_ms: Optional[int] = None) -> tuple[int, int]:
    """``[start_ms, end_ms)`` of the current UTC calendar month."""
    now = (
        datetime.fromtimestamp(int(now_ms) / 1000, tz=timezone.utc)
        if now_ms is not None
        else datetime.now(timezone.utc)
    )
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return int(first.timestamp() * 1000), int(nxt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Channel context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelContext:
    """A project's channel-scoped effective profile (see module docs)."""

    project_id: str
    channel_id: str
    schedule_id: Optional[str] = None
    #: Effective niche profile id + which precedence slot supplied it:
    #: "schedule" | "project" | "channel_default" | None.
    niche_profile_id: Optional[str] = None
    niche_source: Optional[str] = None
    #: True when projects.schedule_id points at another channel's
    #: schedule; that schedule contributed nothing to this context.
    schedule_channel_mismatch: bool = False
    voice_id: Optional[str] = None
    voice_style: Optional[str] = None
    #: "niche_profile.default_voice_id" when a voice id was resolved,
    #: else None (downstream falls back to the voice_style hint).
    voice_source: Optional[str] = None
    words_per_minute: Optional[int] = None
    publish_mode: Optional[str] = None
    monthly_budget_micros: Optional[int] = None
    daily_cost_cap_micros: Optional[int] = None
    daily_upload_cap: Optional[int] = None
    #: The channel's production quality tier (Round 5):
    #: ``channels.quality_tier``, defaulting to ``"draft"`` when the
    #: channel row / column carries nothing (pre-migration databases).
    quality_tier: str = "draft"
    channel: Optional[dict] = field(default=None, repr=False)
    niche: Optional[dict] = field(default=None, repr=False)
    schedule: Optional[dict] = field(default=None, repr=False)
    channel_config: dict = field(default_factory=dict, repr=False)

    # -- convenience views over the channel row -------------------------

    @property
    def youtube_channel_id(self) -> Optional[str]:
        return (self.channel or {}).get("youtube_channel_id")

    @property
    def channel_title(self) -> Optional[str]:
        return (self.channel or {}).get("title")

    @property
    def channel_language(self) -> Optional[str]:
        return (self.channel or {}).get("language")

    @property
    def niche_key(self) -> Optional[str]:
        return (self.niche or {}).get("key")

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for job payloads / integration dispatch."""
        return {
            "project_id": self.project_id,
            "channel_id": self.channel_id,
            "youtube_channel_id": self.youtube_channel_id,
            "channel_title": self.channel_title,
            "channel_language": self.channel_language,
            "schedule_id": self.schedule_id,
            "schedule_channel_mismatch": self.schedule_channel_mismatch,
            "niche_profile_id": self.niche_profile_id,
            "niche_key": self.niche_key,
            "niche_source": self.niche_source,
            "voice_id": self.voice_id,
            "voice_style": self.voice_style,
            "voice_source": self.voice_source,
            "words_per_minute": self.words_per_minute,
            "publish_mode": self.publish_mode,
            "monthly_budget_micros": self.monthly_budget_micros,
            "daily_cost_cap_micros": self.daily_cost_cap_micros,
            "daily_upload_cap": self.daily_upload_cap,
            "quality_tier": self.quality_tier,
        }


def resolve_channel_context_from_conn(
    conn: sqlite3.Connection, project_id: Any
) -> Optional[ChannelContext]:
    """Resolve the channel context for ``project_id`` on an open conn.

    The connection is caller-owned and never closed here.  Returns
    ``None`` when the project does not exist; a missing channel /
    schedule / niche row degrades to a context built from whatever
    rows do exist (ids preserved) rather than raising.
    """
    try:
        conn.row_factory = sqlite3.Row
    except Exception:
        pass
    project = _row_dict(
        conn.execute(
            "SELECT * FROM projects WHERE id = ?", (str(project_id),)
        ).fetchone()
    )
    if project is None:
        return None

    channel_id = str(project.get("channel_id") or "")
    channel: Optional[dict] = None
    if channel_id and _table_exists(conn, "channels"):
        channel = _row_dict(
            conn.execute(
                "SELECT * FROM channels WHERE id = ?", (channel_id,)
            ).fetchone()
        )
    channel_config = parse_channel_config(
        (channel or {}).get("config_json")
    )

    # Schedule: only a schedule owned by the same channel may supply
    # niche / budget / publish-mode values (cross-talk guard).
    schedule: Optional[dict] = None
    mismatch = False
    schedule_id = project.get("schedule_id")
    if schedule_id and _table_exists(conn, "schedules"):
        candidate = _row_dict(
            conn.execute(
                "SELECT * FROM schedules WHERE id = ?",
                (str(schedule_id),),
            ).fetchone()
        )
        if candidate is not None:
            if str(candidate.get("channel_id") or "") == channel_id:
                schedule = candidate
            else:
                mismatch = True

    # Niche precedence: schedule > project > channel default.
    niche_id: Optional[str] = None
    niche_source: Optional[str] = None
    if schedule is not None and schedule.get("niche_profile_id"):
        niche_id = str(schedule["niche_profile_id"])
        niche_source = "schedule"
    elif project.get("niche_profile_id"):
        niche_id = str(project["niche_profile_id"])
        niche_source = "project"
    elif channel is not None and channel.get("default_niche_profile_id"):
        niche_id = str(channel["default_niche_profile_id"])
        niche_source = "channel_default"

    niche: Optional[dict] = None
    if niche_id and _table_exists(conn, "niche_profiles"):
        niche = _row_dict(
            conn.execute(
                "SELECT * FROM niche_profiles WHERE id = ?", (niche_id,)
            ).fetchone()
        )

    voice_id = None
    voice_source = None
    if niche is not None:
        raw_voice = niche.get("default_voice_id")
        if isinstance(raw_voice, str) and raw_voice.strip():
            voice_id = raw_voice.strip()
            voice_source = "niche_profile.default_voice_id"

    return ChannelContext(
        project_id=str(project.get("id") or project_id),
        channel_id=channel_id,
        schedule_id=str(schedule["id"]) if schedule is not None else (
            str(schedule_id) if schedule_id else None
        ),
        niche_profile_id=niche_id,
        niche_source=niche_source,
        schedule_channel_mismatch=mismatch,
        voice_id=voice_id,
        voice_style=(niche or {}).get("voice_style"),
        voice_source=voice_source,
        words_per_minute=_as_int((niche or {}).get("words_per_minute")),
        publish_mode=(
            project.get("publish_mode")
            or (schedule or {}).get("publish_mode")
            or "review_first"
        ),
        monthly_budget_micros=_as_int(
            (channel or {}).get("monthly_budget_micros")
        ),
        daily_cost_cap_micros=_as_int(
            (schedule or {}).get("daily_cost_cap_micros")
        ),
        daily_upload_cap=_as_int((channel or {}).get("daily_upload_cap")),
        quality_tier=_quality_tier_of(channel),
        channel=channel,
        niche=niche,
        schedule=schedule,
        channel_config=channel_config,
    )


def resolve_channel_context(
    conn_factory: Any, project_id: Any
) -> Optional[ChannelContext]:
    """Resolve the channel context for ``project_id``.

    ``conn_factory`` may be a zero-arg callable returning a fresh
    ``sqlite3.Connection`` (the ``app.db`` / execution-layer pattern —
    the connection is closed here), a bare connection (caller-owned,
    never closed), or ``None``-returning on failure (yields ``None``).
    Mirrors the factory/connection duality of
    :mod:`app.execution.gates_bridge`.
    """
    if conn_factory is None:
        return None
    if hasattr(conn_factory, "execute"):
        return resolve_channel_context_from_conn(conn_factory, project_id)
    if not callable(conn_factory):
        raise TypeError("conn_factory must be callable or a DB connection")
    conn = conn_factory()
    if conn is None:
        return None
    # Probe for the tests' ``lambda: conn`` shared-connection pattern:
    # a second call returning the same object means the caller owns
    # the connection's lifecycle and we must not close it.
    should_close = True
    try:
        probe = conn_factory()
        if probe is conn:
            should_close = False
        else:
            try:
                probe.close()
            except Exception:
                pass
    except Exception:
        should_close = True
    try:
        return resolve_channel_context_from_conn(conn, project_id)
    finally:
        if should_close:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Per-channel usage / budget view
# ---------------------------------------------------------------------------


def _sum_for_channel(
    conn: sqlite3.Connection,
    table: str,
    channel_id: str,
    start_ms: int,
    end_ms: int,
) -> int:
    """SUM(cost_micros) in ``table`` for one channel via its projects."""
    if not _table_exists(conn, table) or not _table_exists(conn, "projects"):
        return 0
    try:
        row = conn.execute(
            f"SELECT COALESCE(SUM(t.cost_micros), 0) FROM {table} t "
            "JOIN projects p ON p.id = t.project_id "
            "WHERE p.channel_id = ? AND t.created_at >= ? "
            "AND t.created_at < ?",
            (channel_id, start_ms, end_ms),
        ).fetchone()
    except Exception:
        return 0
    return int(row[0] or 0) if row is not None else 0


def build_channel_usage_view(
    conn: sqlite3.Connection,
    channel_id: Any,
    *,
    now_ms: Optional[int] = None,
) -> Optional[dict]:
    """Usage/budget view for one channel (internal id or YouTube id).

    Returns ``None`` when no live channel row matches.  All money is
    integer micros plus a rounded ``*_usd`` convenience mirror,
    matching the existing usage-router style.
    """
    if not _table_exists(conn, "channels"):
        return None
    try:
        conn.row_factory = sqlite3.Row
    except Exception:
        pass
    row = conn.execute(
        "SELECT * FROM channels WHERE (id = ? OR youtube_channel_id = ?) "
        "AND deleted_at IS NULL LIMIT 1",
        (str(channel_id), str(channel_id)),
    ).fetchone()
    channel = _row_dict(row)
    if channel is None:
        return None
    cid = str(channel["id"])

    start_ms, end_ms = month_window_ms(now_ms)
    month_start_iso = datetime.fromtimestamp(
        start_ms / 1000, tz=timezone.utc
    ).date().isoformat()

    mtd_spend = _sum_for_channel(conn, "agent_runs", cid, start_ms, end_ms)
    mtd_api = _sum_for_channel(conn, "api_usage", cid, start_ms, end_ms)

    projects_total = 0
    counts: dict[str, int] = {}
    project_count = 0
    if _table_exists(conn, "projects"):
        try:
            prow = conn.execute(
                "SELECT COALESCE(SUM(total_cost_micros), 0) FROM projects "
                "WHERE channel_id = ? AND deleted_at IS NULL",
                (cid,),
            ).fetchone()
            projects_total = int(prow[0] or 0) if prow is not None else 0
            for srow in conn.execute(
                "SELECT status, COUNT(*) FROM projects "
                "WHERE channel_id = ? AND deleted_at IS NULL "
                "GROUP BY status",
                (cid,),
            ).fetchall():
                counts[str(srow[0])] = int(srow[1])
                project_count += int(srow[1])
        except Exception:
            projects_total, counts, project_count = 0, {}, 0

    schedules: list[dict] = []
    if _table_exists(conn, "schedules"):
        try:
            for srow in conn.execute(
                "SELECT * FROM schedules WHERE channel_id = ? "
                "AND deleted_at IS NULL ORDER BY created_at ASC, id ASC",
                (cid,),
            ).fetchall():
                s = _row_dict(srow) or {}
                schedules.append(
                    {
                        "schedule_id": s.get("id"),
                        "name": s.get("name"),
                        "niche_profile_id": s.get("niche_profile_id"),
                        "publish_mode": s.get("publish_mode"),
                        "daily_cost_cap_micros": _as_int(
                            s.get("daily_cost_cap_micros")
                        ),
                        "daily_cost_cap_usd": (
                            round(
                                int(s["daily_cost_cap_micros"]) / 1_000_000, 6
                            )
                            if _as_int(s.get("daily_cost_cap_micros"))
                            is not None
                            else None
                        ),
                        "is_enabled": bool(s.get("is_enabled")),
                    }
                )
        except Exception:
            schedules = []

    monthly_cap = _as_int(channel.get("monthly_budget_micros"))
    remaining = (
        max(0, monthly_cap - mtd_spend) if monthly_cap is not None else None
    )
    return {
        "channel_id": cid,
        "youtube_channel_id": channel.get("youtube_channel_id"),
        "title": channel.get("title"),
        "status": channel.get("status"),
        "language": channel.get("language"),
        "default_niche_profile_id": channel.get("default_niche_profile_id"),
        "quality_tier": _quality_tier_of(channel),
        "daily_upload_cap": _as_int(channel.get("daily_upload_cap")),
        "monthly_budget_micros": monthly_cap,
        "monthly_budget_usd": (
            round(monthly_cap / 1_000_000, 6)
            if monthly_cap is not None
            else None
        ),
        "month": month_start_iso,
        "month_start_ms": start_ms,
        "month_end_ms": end_ms,
        "month_to_date_spend_micros": mtd_spend,
        "month_to_date_spend_usd": round(mtd_spend / 1_000_000, 6),
        "month_to_date_api_usage_micros": mtd_api,
        "month_to_date_api_usage_usd": round(mtd_api / 1_000_000, 6),
        "monthly_budget_remaining_micros": remaining,
        "monthly_budget_remaining_usd": (
            round(remaining / 1_000_000, 6) if remaining is not None else None
        ),
        "projects_total_cost_micros": projects_total,
        "projects_total_cost_usd": round(projects_total / 1_000_000, 6),
        "project_count": project_count,
        "project_counts_by_status": counts,
        "schedules": schedules,
        "schedule_daily_caps_micros": [
            s["daily_cost_cap_micros"] for s in schedules
        ],
    }


def build_channel_usage_views(
    conn: sqlite3.Connection, *, now_ms: Optional[int] = None
) -> list[dict]:
    """Usage/budget views for every live channel (creation order)."""
    if not _table_exists(conn, "channels"):
        return []
    try:
        rows = conn.execute(
            "SELECT id FROM channels WHERE deleted_at IS NULL "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()
    except Exception:
        return []
    views: list[dict] = []
    for row in rows:
        view = build_channel_usage_view(conn, row[0], now_ms=now_ms)
        if view is not None:
            views.append(view)
    return views
