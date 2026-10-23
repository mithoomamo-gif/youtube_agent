"""Per-channel production quality tier (Round 5 — master encode).

The channel's output quality is a real column on the ``channels``
table — ``channels.quality_tier`` — following the same storage
pattern as the channel's other first-class settings
(``daily_upload_cap``, ``monthly_budget_micros``,
``default_niche_profile_id``):

* Alembic migration ``0008_channel_quality_tier`` adds the column to
  existing databases (guarded ``ADD COLUMN``, mirroring 0002's
  ``last4`` addition);
* :mod:`app.schema_ddl` carries the column in the ``channels`` DDL,
  so fresh databases created by ``app.db.init_db`` have it from the
  start;
* :func:`ensure_quality_tier_column` adds it lazily on a live
  connection, so every reader/writer below works on a database that
  has not run migrations yet (the house pattern — see
  ``app.style_lab.schema.ensure_style_lab_schema`` and
  ``app.execution.budget_reservations.ensure_reservations_table``).

Vocabulary (exactly the render path's ``quality_tier`` parameter —
``app.pipeline.render.renderer.QUALITY_TIERS``):

* ``draft`` (DEFAULT for every existing and new channel — behaviour
  does not change until a user flips the setting);
* ``master_1080p``;
* ``master_1440p_upscale``.

Plumbing seam: the tier is resolved from the channel row at the two
spec-construction seams and carried on
:class:`app.integration.models.ProjectSpec` as ``quality_tier``:

* manual / DAG path — ``app.execution.handlers`` resolves the
  project's :class:`~app.execution.channel_context.ChannelContext`
  (which now carries ``quality_tier``) and
  ``app.integration.stage_bridge.project_to_spec`` maps the channel
  row (or the injected channel context / an explicit payload value)
  onto the spec;
* scheduled path — ``app.execution.scheduler.SchedulerTick``
  materialises the project with the schedule's ``channel_id``; from
  there the same handlers -> stage_bridge path resolves the tier
  from that channel's row, so a scheduled video is rendered at
  exactly its channel's tier.

From the spec, ``app.integration.produce`` passes ``quality_tier``
into the assemble/render entry point
(``assemble_stage.assemble_project(..., quality_tier=...)``), whose
own fallback (``ctx.spec.quality_tier``) reads the same field.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

__all__ = [
    "QUALITY_TIERS",
    "DEFAULT_QUALITY_TIER",
    "QUALITY_TIER_COLUMN",
    "QUALITY_TIER_COLUMN_DDL",
    "is_valid_quality_tier",
    "normalize_quality_tier",
    "quality_tier_of_row",
    "ensure_quality_tier_column",
    "resolve_channel_quality_tier",
]

#: The exact tier vocabulary of the render path's ``quality_tier``
#: parameter (kept in sync with
#: ``app.pipeline.render.renderer.QUALITY_TIERS``).
QUALITY_TIERS: tuple[str, ...] = (
    "draft",
    "master_1080p",
    "master_1440p_upscale",
)

#: Every existing and new channel defaults to the historical
#: behaviour until a user flips the setting.
DEFAULT_QUALITY_TIER: str = "draft"

QUALITY_TIER_COLUMN: str = "quality_tier"

#: Column definition used by the ADD COLUMN paths (migration 0008 +
#: :func:`ensure_quality_tier_column`).  The same definition is
#: written literally into the ``channels`` DDL in app.schema_ddl.
QUALITY_TIER_COLUMN_DDL: str = (
    "TEXT NOT NULL DEFAULT 'draft' "
    "CHECK (quality_tier IN ('draft', 'master_1080p', "
    "'master_1440p_upscale'))"
)


def is_valid_quality_tier(value: Any) -> bool:
    return isinstance(value, str) and value.strip() in QUALITY_TIERS


def normalize_quality_tier(
    value: Any, *, default: Optional[str] = None
) -> str:
    """Canonical tier name for ``value``.

    ``None`` / empty resolves to ``default`` (or
    :data:`DEFAULT_QUALITY_TIER` when no default is given).  An
    unknown non-empty value raises :class:`ValueError` — a misspelled
    tier must fail loud (R4-003), never silently render a draft.
    """
    if value is None:
        return default if default is not None else DEFAULT_QUALITY_TIER
    cleaned = str(value).strip().lower()
    if not cleaned:
        return default if default is not None else DEFAULT_QUALITY_TIER
    if cleaned not in QUALITY_TIERS:
        raise ValueError(
            f"unknown quality_tier {value!r}; "
            f"expected one of {', '.join(QUALITY_TIERS)}"
        )
    return cleaned


def quality_tier_of_row(row: Any) -> str:
    """The tier stored on a channel row dict, defaulting honestly.

    Missing column / NULL / unrecognised stored value -> ``draft``
    (the pre-Round-5 behaviour); a stored valid value is returned
    verbatim (normalised).
    """
    if row is None:
        return DEFAULT_QUALITY_TIER
    raw = row.get(QUALITY_TIER_COLUMN) if isinstance(row, dict) else None
    if raw is None:
        try:
            raw = row[QUALITY_TIER_COLUMN]  # sqlite3.Row access
        except Exception:
            raw = None
    try:
        return normalize_quality_tier(raw)
    except ValueError:
        return DEFAULT_QUALITY_TIER


def _table_exists(conn: Any, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _column_names(conn: Any, table: str) -> set:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def ensure_quality_tier_column(conn: Any) -> bool:
    """Add ``channels.quality_tier`` to ``conn``'s DB when missing.

    Idempotent; returns True when the column is present afterwards.
    Mirrors migration 0008's guarded ALTER (SQLite has no
    ``ADD COLUMN IF NOT EXISTS``).  Existing rows receive the column
    DEFAULT (``draft``), so behaviour is unchanged until a user
    flips the setting.
    """
    if not _table_exists(conn, "channels"):
        return False
    if QUALITY_TIER_COLUMN in _column_names(conn, "channels"):
        return True
    conn.execute(
        f"ALTER TABLE channels ADD COLUMN {QUALITY_TIER_COLUMN} "
        f"{QUALITY_TIER_COLUMN_DDL}"
    )
    try:
        conn.commit()
    except Exception:
        pass
    return QUALITY_TIER_COLUMN in _column_names(conn, "channels")


def resolve_channel_quality_tier(conn: Any, channel_id: Any) -> str:
    """The stored tier for one channel (internal id OR YouTube id).

    Unknown channel / missing table / missing value -> ``draft``.
    The connection is caller-owned and never closed here.  The
    column is ensured first (lazily), so callers work against
    databases that have not run migration 0008.
    """
    if conn is None or channel_id is None or not str(channel_id).strip():
        return DEFAULT_QUALITY_TIER
    if not _table_exists(conn, "channels"):
        return DEFAULT_QUALITY_TIER
    ensure_quality_tier_column(conn)
    if QUALITY_TIER_COLUMN not in _column_names(conn, "channels"):
        return DEFAULT_QUALITY_TIER
    try:
        row = conn.execute(
            "SELECT quality_tier FROM channels "
            "WHERE (id = ? OR youtube_channel_id = ?) "
            "AND deleted_at IS NULL LIMIT 1",
            (str(channel_id), str(channel_id)),
        ).fetchone()
    except sqlite3.Error:
        # A channels table without deleted_at (hand-built fixtures):
        # retry without the soft-delete filter.
        try:
            row = conn.execute(
                "SELECT quality_tier FROM channels "
                "WHERE (id = ? OR youtube_channel_id = ?) LIMIT 1",
                (str(channel_id), str(channel_id)),
            ).fetchone()
        except Exception:
            return DEFAULT_QUALITY_TIER
    except Exception:
        return DEFAULT_QUALITY_TIER
    if row is None:
        return DEFAULT_QUALITY_TIER
    return quality_tier_of_row({"quality_tier": row[0]})
