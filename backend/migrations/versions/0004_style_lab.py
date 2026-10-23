"""Style Lab competitor intake + style profiles persistence.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23

Changes (Style Lab mini-round — intake + storage):
- New ``competitor_channels`` table: one row per competitor channel
  the user points at, scoped to one of the user's own channels via
  ``channel_id`` (REFERENCES channels ON DELETE RESTRICT — the owner
  channel is the scope; deleting it must not silently orphan or wipe
  competitor records).  ``source_input`` keeps the verbatim user
  input and ``source_kind`` records how it was classified
  ('channel_url' / 'handle' / 'channel_id' / 'video_url' /
  'custom_url'); ``external_channel_id`` (UC…), ``handle`` (normalised,
  no '@'), ``display_name`` and ``channel_url`` are filled in when
  public metadata is fetched, with ``metadata_status`` tracking that
  fetch ('not_fetched' / 'fetched' / 'metadata_unavailable' /
  'fetch_failed') and ``metadata_json`` holding the raw public
  metadata.  ``status`` is 'active' / 'archived'.
- New ``competitor_videos`` table: one row per competitor video
  considered for a channel (REFERENCES competitor_channels ON DELETE
  RESTRICT).  ``source`` records how the video arrived:
  'attached_file' (user-supplied file — ``storage_path`` is relative
  to the app data dir, with ``file_sha256`` / ``file_size_bytes`` /
  ``mime_type`` / ``duration_s`` / ``width`` / ``height`` /
  ``thumbnail_storage_path`` / ``thumbnail_sha256`` describing the
  stored media), 'url_metadata' or 'pasted_metadata' (public or
  user-pasted ``title`` / ``description`` / ``tags_json`` only).
  ``analysis_status`` tracks the analysis lifecycle ('not_queued' /
  'queued' / 'analysing' / 'analysed' / 'analysis_failed'), with
  ``analysis_requested_at`` stamping when analysis was requested.
- New ``style_profiles`` table: one row per distilled style profile,
  scoped to the user's channel via ``channel_id`` (REFERENCES
  channels ON DELETE RESTRICT).  ``competitor_ids_json`` lists the
  competitor_channels rows the profile was distilled from and
  ``profile_json`` holds the profile itself; ``version`` increments
  per revision and ``status`` is 'draft' / 'active' / 'superseded'
  (at most one active profile per channel is a service-level rule,
  not a schema constraint).  Timestamps are INTEGER Unix epoch
  milliseconds, matching the section-15 global conventions in
  app.schema_ddl.

Style Lab learns style / formula only — structure, pacing, hook
patterns, visual-source mix, title/description patterns.  It never
reuses a competitor's footage, script text, exact titles, thumbnails
or branding.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with app.style_lab.schema (the runtime module ensures
# the tables exist whether or not alembic ran).
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


def upgrade() -> None:
    op.execute(COMPETITOR_CHANNELS_DDL)
    op.execute(COMPETITOR_VIDEOS_DDL)
    op.execute(STYLE_PROFILES_DDL)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_competitor_channels_channel_status "
        "ON competitor_channels (channel_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_competitor_videos_channel "
        "ON competitor_videos (competitor_channel_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_competitor_videos_analysis_status "
        "ON competitor_videos (analysis_status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_style_profiles_channel_status "
        "ON style_profiles (channel_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_style_profiles_channel_status")
    op.execute("DROP INDEX IF EXISTS idx_competitor_videos_analysis_status")
    op.execute("DROP INDEX IF EXISTS idx_competitor_videos_channel")
    op.execute("DROP INDEX IF EXISTS idx_competitor_channels_channel_status")
    op.execute("DROP TABLE IF EXISTS style_profiles")
    op.execute("DROP TABLE IF EXISTS competitor_videos")
    op.execute("DROP TABLE IF EXISTS competitor_channels")
