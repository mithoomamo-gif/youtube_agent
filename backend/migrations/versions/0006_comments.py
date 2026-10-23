"""Comments Intelligence persistence (Round 5 — Agent 28).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-23

Changes (Round 5 — Comments Intelligence, schema + storage foundation):
- New ``comments`` table: one row per YouTube comment synced for one
  of the user's own videos.  ``id`` IS the YouTube comment id
  (PRIMARY KEY) — the idempotency key: re-sync upserts the same row
  and refreshes mutable counters (``like_count`` / ``reply_count`` /
  ``text`` / ``fetched_at``), never duplicates.  ``channel_id``
  (REFERENCES channels ON DELETE RESTRICT) is the owner scope;
  ``video_id`` is the YouTube video id (TEXT, not an FK — comments
  may be synced for a video whose project row is gone) and
  ``project_id`` optionally links our internal ``projects`` row
  (ON DELETE SET NULL).  ``author_display_name`` /
  ``author_channel_id`` are the public fields exactly as returned by
  the API; ``parent_comment_id`` + ``is_top_level`` carry reply
  threading; ``published_at`` is INTEGER epoch ms (converted from the
  API's RFC3339 timestamp); ``raw_json`` is an optional debug stash
  that nothing may depend on.
- New ``comment_sync_state`` table: PRIMARY KEY (channel_id,
  video_id); ``status`` is the honest-states backbone —
  'never_synced' / 'synced' / 'not_connected' / 'error' — with
  ``last_synced_at``, ``comment_count`` and ``error_reason``.
- New ``comment_analyses`` table: per-video or per-channel rollups
  (``scope`` 'video' / 'channel'; a video-scope row must carry a
  ``video_id``), ``rollup_json`` holds the rollup, and provenance is
  columns: ``engine`` ('llm' / 'deterministic'), ``model_id``,
  ``comment_count``, ``provenance_json``.
- New ``comment_drafts`` table: Agent 28 reply drafts per comment
  (REFERENCES comments ON DELETE CASCADE; ``channel_id`` +
  ``video_id`` denormalised for owner scoping).  ``status`` is
  'proposed' / 'approved' / 'rejected' — the store enforces
  proposed -> approved|rejected only; terminal states are immutable.
  ``created_by`` is 'agent28' (or 'user').
- New ``comment_moderation_flags`` table: spam / abuse / other flags
  per comment with a free-form advisory ``suggested_action`` and
  ``status`` 'pending' / 'dismissed' / 'actioned'.  Flags NEVER
  auto-execute an action — the status only records a human decision.

Timestamps are INTEGER Unix epoch milliseconds, matching the
section-15 global conventions in app.schema_ddl.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept byte-identical with app.comments.schema (the runtime module
# ensures the tables exist whether or not alembic ran).
COMMENTS_DDL = """
CREATE TABLE IF NOT EXISTS comments (
    id                  TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL
                        REFERENCES channels (id) ON DELETE RESTRICT,
    video_id            TEXT NOT NULL,
    project_id          TEXT NULL
                        REFERENCES projects (id) ON DELETE SET NULL,
    author_display_name TEXT NULL,
    author_channel_id   TEXT NULL,
    text                TEXT NOT NULL,
    like_count          INTEGER NOT NULL DEFAULT 0,
    reply_count         INTEGER NOT NULL DEFAULT 0,
    parent_comment_id   TEXT NULL,
    is_top_level        INTEGER NOT NULL DEFAULT 1
                        CHECK (is_top_level IN (0, 1)),
    published_at        INTEGER NULL,
    fetched_at          INTEGER NOT NULL,
    raw_json            TEXT NULL,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
)
"""

COMMENT_SYNC_STATE_DDL = """
CREATE TABLE IF NOT EXISTS comment_sync_state (
    channel_id     TEXT NOT NULL
                   REFERENCES channels (id) ON DELETE RESTRICT,
    video_id       TEXT NOT NULL,
    project_id     TEXT NULL
                   REFERENCES projects (id) ON DELETE SET NULL,
    status         TEXT NOT NULL DEFAULT 'never_synced'
                   CHECK (status IN ('never_synced', 'synced',
                          'not_connected', 'error')),
    last_synced_at INTEGER NULL,
    comment_count  INTEGER NOT NULL DEFAULT 0,
    error_reason   TEXT NULL,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (channel_id, video_id)
)
"""

COMMENT_ANALYSES_DDL = """
CREATE TABLE IF NOT EXISTS comment_analyses (
    id              TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL
                    REFERENCES channels (id) ON DELETE RESTRICT,
    scope           TEXT NOT NULL DEFAULT 'video'
                    CHECK (scope IN ('video', 'channel')),
    video_id        TEXT NULL,
    rollup_json     TEXT NULL,
    engine          TEXT NOT NULL
                    CHECK (engine IN ('llm', 'deterministic')),
    model_id        TEXT NULL,
    comment_count   INTEGER NOT NULL DEFAULT 0,
    provenance_json TEXT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    CHECK ((scope = 'video' AND video_id IS NOT NULL)
           OR scope = 'channel')
)
"""

COMMENT_DRAFTS_DDL = """
CREATE TABLE IF NOT EXISTS comment_drafts (
    id              TEXT PRIMARY KEY,
    comment_id      TEXT NOT NULL
                    REFERENCES comments (id) ON DELETE CASCADE,
    channel_id      TEXT NOT NULL
                    REFERENCES channels (id) ON DELETE RESTRICT,
    video_id        TEXT NOT NULL,
    draft_text      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'approved', 'rejected',
                           'discarded')),
    created_by      TEXT NOT NULL DEFAULT 'agent28'
                    CHECK (created_by IN ('agent28', 'user')),
    provenance_json TEXT NULL,
    decided_at      INTEGER NULL,
    decided_by      TEXT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
)
"""

COMMENT_MODERATION_FLAGS_DDL = """
CREATE TABLE IF NOT EXISTS comment_moderation_flags (
    id               TEXT PRIMARY KEY,
    comment_id       TEXT NOT NULL
                     REFERENCES comments (id) ON DELETE CASCADE,
    channel_id       TEXT NOT NULL
                     REFERENCES channels (id) ON DELETE RESTRICT,
    video_id         TEXT NOT NULL,
    flag_kind        TEXT NOT NULL DEFAULT 'other'
                     CHECK (flag_kind IN ('spam', 'abuse', 'other')),
    reason           TEXT NULL,
    suggested_action TEXT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'dismissed', 'actioned')),
    provenance_json  TEXT NULL,
    decided_at       INTEGER NULL,
    decided_by       TEXT NULL,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
)
"""


def upgrade() -> None:
    op.execute(COMMENTS_DDL)
    op.execute(COMMENT_SYNC_STATE_DDL)
    op.execute(COMMENT_ANALYSES_DDL)
    op.execute(COMMENT_DRAFTS_DDL)
    op.execute(COMMENT_MODERATION_FLAGS_DDL)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_comments_channel_video "
        "ON comments (channel_id, video_id, published_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_comments_channel_published "
        "ON comments (channel_id, published_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_comment_sync_state_channel "
        "ON comment_sync_state (channel_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_comment_analyses_channel_video "
        "ON comment_analyses (channel_id, video_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_comment_analyses_channel_scope "
        "ON comment_analyses (channel_id, scope, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_comment_drafts_comment "
        "ON comment_drafts (comment_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_comment_drafts_channel_status "
        "ON comment_drafts (channel_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_comment_moderation_flags_comment "
        "ON comment_moderation_flags (comment_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_comment_moderation_flags_channel_status "
        "ON comment_moderation_flags (channel_id, status)"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS idx_comment_moderation_flags_channel_status"
    )
    op.execute("DROP INDEX IF EXISTS idx_comment_moderation_flags_comment")
    op.execute("DROP INDEX IF EXISTS idx_comment_drafts_channel_status")
    op.execute("DROP INDEX IF EXISTS idx_comment_drafts_comment")
    op.execute("DROP INDEX IF EXISTS idx_comment_analyses_channel_scope")
    op.execute("DROP INDEX IF EXISTS idx_comment_analyses_channel_video")
    op.execute("DROP INDEX IF EXISTS idx_comment_sync_state_channel")
    op.execute("DROP INDEX IF EXISTS idx_comments_channel_published")
    op.execute("DROP INDEX IF EXISTS idx_comments_channel_video")
    op.execute("DROP TABLE IF EXISTS comment_moderation_flags")
    op.execute("DROP TABLE IF EXISTS comment_drafts")
    op.execute("DROP TABLE IF EXISTS comment_analyses")
    op.execute("DROP TABLE IF EXISTS comment_sync_state")
    op.execute("DROP TABLE IF EXISTS comments")
