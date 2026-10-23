"""Style Lab tables — runtime DDL + lazy schema creation.

This module holds the DDL for the three Style Lab tables
(``competitor_channels``, ``competitor_videos``, ``style_profiles``)
and :func:`ensure_style_lab_schema`, which creates them lazily on a
connection — the pattern used by :mod:`app.routers.approvals`
(``_ensure_approvals_table``) and
:mod:`app.execution.budget_reservations`
(``ensure_reservations_table``) — so Style Lab works on databases
that have not run migrations.

The DDL constants below are kept in sync with Alembic migration
``0004_style_lab`` (``backend/migrations/versions/0004_style_lab.py``),
which carries the same constants under the same names.

Schema notes (the shared Style Lab contract):

* TEXT primary keys are uuid4 hex; timestamps are INTEGER Unix epoch
  milliseconds, matching the section-15 global conventions in
  :mod:`app.schema_ddl`.
* ``competitor_channels.channel_id`` and ``style_profiles.channel_id``
  are the *scope*: REFERENCES channels (id) ON DELETE RESTRICT — the
  owner is the user's own channel the competitor / profile belongs
  to.  ``competitor_videos.competitor_channel_id`` REFERENCES
  competitor_channels (id) ON DELETE RESTRICT.
* ``competitor_videos.storage_path`` / ``thumbnail_storage_path``
  are relative to the app data dir.
* Enumerations are enforced with CHECK constraints; see the DDL.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "COMPETITOR_CHANNELS_TABLE",
    "COMPETITOR_VIDEOS_TABLE",
    "STYLE_PROFILES_TABLE",
    "COMPETITOR_CHANNELS_DDL",
    "COMPETITOR_VIDEOS_DDL",
    "STYLE_PROFILES_DDL",
    "STYLE_LAB_DDL",
    "INDEX_STATEMENTS",
    "ensure_style_lab_schema",
]

COMPETITOR_CHANNELS_TABLE = "competitor_channels"
COMPETITOR_VIDEOS_TABLE = "competitor_videos"
STYLE_PROFILES_TABLE = "style_profiles"

# Kept in sync with migrations/versions/0004_style_lab.py (the
# migration carries the same constants under the same names).
COMPETITOR_CHANNELS_DDL = """
CREATE TABLE IF NOT EXISTS competitor_channels (
    id                TEXT PRIMARY KEY,
    channel_id        TEXT NOT NULL
                      REFERENCES channels (id) ON DELETE RESTRICT,
    platform          TEXT NOT NULL DEFAULT 'youtube'
                      CHECK (platform IN ('youtube')),
    source_input      TEXT NOT NULL,
    source_kind       TEXT NOT NULL
                      CHECK (source_kind IN ('channel_url', 'handle',
                             'channel_id', 'video_url', 'custom_url')),
    external_channel_id TEXT NULL,
    handle            TEXT NULL,
    display_name      TEXT NULL,
    channel_url       TEXT NULL,
    metadata_status   TEXT NOT NULL DEFAULT 'not_fetched'
                      CHECK (metadata_status IN ('not_fetched', 'fetched',
                             'metadata_unavailable', 'fetch_failed')),
    metadata_json     TEXT NULL,
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'archived')),
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
)
"""

COMPETITOR_VIDEOS_DDL = """
CREATE TABLE IF NOT EXISTS competitor_videos (
    id                      TEXT PRIMARY KEY,
    competitor_channel_id   TEXT NOT NULL
                            REFERENCES competitor_channels (id)
                            ON DELETE RESTRICT,
    source                  TEXT NOT NULL
                            CHECK (source IN ('attached_file',
                                   'url_metadata', 'pasted_metadata')),
    external_video_id       TEXT NULL,
    video_url               TEXT NULL,
    title                   TEXT NULL,
    description             TEXT NULL,
    tags_json               TEXT NULL,
    storage_path            TEXT NULL,
    file_sha256             TEXT NULL,
    file_size_bytes         INTEGER NULL,
    mime_type               TEXT NULL,
    duration_s              REAL NULL,
    width                   INTEGER NULL,
    height                  INTEGER NULL,
    thumbnail_storage_path  TEXT NULL,
    thumbnail_sha256        TEXT NULL,
    analysis_status         TEXT NOT NULL DEFAULT 'not_queued'
                            CHECK (analysis_status IN ('not_queued',
                                   'queued', 'analysing', 'analysed',
                                   'analysis_failed')),
    analysis_requested_at   INTEGER NULL,
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL
)
"""

STYLE_PROFILES_DDL = """
CREATE TABLE IF NOT EXISTS style_profiles (
    id                  TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL
                        REFERENCES channels (id) ON DELETE RESTRICT,
    name                TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'active', 'superseded')),
    competitor_ids_json TEXT NOT NULL DEFAULT '[]',
    profile_json        TEXT NULL,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
)
"""

#: All three table DDL statements, in creation (FK dependency) order.
STYLE_LAB_DDL = (
    COMPETITOR_CHANNELS_DDL,
    COMPETITOR_VIDEOS_DDL,
    STYLE_PROFILES_DDL,
)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_competitor_channels_channel_status "
    "ON competitor_channels (channel_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_competitor_videos_channel "
    "ON competitor_videos (competitor_channel_id)",
    "CREATE INDEX IF NOT EXISTS idx_competitor_videos_analysis_status "
    "ON competitor_videos (analysis_status)",
    "CREATE INDEX IF NOT EXISTS idx_style_profiles_channel_status "
    "ON style_profiles (channel_id, status)",
)

# Alias following the budget_reservations naming pattern.
_INDEX_STATEMENTS = INDEX_STATEMENTS


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def ensure_style_lab_schema(conn: Any) -> None:
    """Create the Style Lab tables + indexes on ``conn`` if absent.

    Idempotent (every statement is IF NOT EXISTS) and safe to call on
    every store / router entry point, mirroring
    :func:`app.execution.budget_reservations.ensure_reservations_table`.
    """
    for statement in STYLE_LAB_DDL:
        conn.execute(statement)
    for statement in INDEX_STATEMENTS:
        conn.execute(statement)
    _commit_quietly(conn)
