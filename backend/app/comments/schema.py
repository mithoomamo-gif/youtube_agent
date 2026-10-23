"""Comments Intelligence tables — runtime DDL + lazy schema creation.

This module holds the DDL for the five Round 5 Comments Intelligence
(Agent 28) tables (``comments``, ``comment_sync_state``,
``comment_analyses``, ``comment_drafts``,
``comment_moderation_flags``) and :func:`ensure_comments_schema`,
which creates them lazily on a connection — the pattern used by
:mod:`app.style_lab.schema` (``ensure_style_lab_schema``) — so the
comments store works on databases that have not run migrations.

The DDL constants below are kept BYTE-IDENTICAL with Alembic
migration ``0006_comments`` (``backend/migrations/versions/
0006_comments.py``), which carries the same constants under the same
names.  The schema test asserts that equality.  ONE deliberate
exception (R5-004 closure): ``COMMENT_DRAFTS_DDL``'s status CHECK
now also admits ``'discarded'``; the migration copy still has the
old CHECK, so the byte-identity / parity assertions for that one
constant are expected to fail until the migration owner aligns
``0006_comments`` (or a later migration widens the CHECK).

Schema notes (the shared Comments contract):

* Timestamps are INTEGER Unix epoch milliseconds, matching the
  section-15 global conventions in :mod:`app.schema_ddl`.
* ``comments.id`` IS the YouTube comment id (PRIMARY KEY) — that is
  the idempotency key: re-syncing a comment upserts the same row and
  refreshes mutable counters (``like_count`` / ``reply_count`` /
  ``text``), it never duplicates.
* Owner scope: every table carries ``channel_id`` REFERENCES
  channels (id) ON DELETE RESTRICT — the user's own channel the
  comment / analysis / draft / flag belongs to.  ``comment_drafts``
  and ``comment_moderation_flags`` additionally denormalise
  ``channel_id`` + ``video_id`` from their parent comment so every
  read/write can be owner-scoped without a join.
* ``comments.video_id`` is the YouTube video id (TEXT, not an FK —
  comments can be synced for a published video whose project row is
  gone or was never ours).  ``project_id`` is the optional link to
  our internal ``projects`` row (ON DELETE SET NULL).
* ``comments.published_at`` is epoch ms (converted from the YouTube
  API's RFC3339 timestamp by the sync worker / store helper).
  ``raw_json`` is deliberately NOT stored by default — the lean
  columns below are the contract; a sync worker MAY stash the raw
  API snippet in ``raw_json`` when debugging, but nothing may depend
  on it being present.
* ``comment_analyses.scope`` is 'video' (``video_id`` required) or
  'channel' (rollup across the channel).  ``engine`` is 'llm' or
  'deterministic' and ``model_id`` records the model when the engine
  is 'llm' — provenance is a column, not a log line.
* ``comment_moderation_flags.flag_kind`` defaults to 'other' so
  kind-less writers (the analysis engine inserts spam flags with only
  comment/channel/video + reason) still land, honestly labelled.
* Moderation flags NEVER auto-execute an action: ``suggested_action``
  is free-form advisory text (e.g. 'review' / 'hide' / 'report' /
  'delete' / 'none') and ``status`` only records the HUMAN decision
  ('pending' -> 'dismissed' | 'actioned').
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "COMMENTS_TABLE",
    "COMMENT_SYNC_STATE_TABLE",
    "COMMENT_ANALYSES_TABLE",
    "COMMENT_DRAFTS_TABLE",
    "COMMENT_MODERATION_FLAGS_TABLE",
    "COMMENTS_DDL",
    "COMMENT_SYNC_STATE_DDL",
    "COMMENT_ANALYSES_DDL",
    "COMMENT_DRAFTS_DDL",
    "COMMENT_MODERATION_FLAGS_DDL",
    "COMMENTS_SCHEMA_DDL",
    "INDEX_STATEMENTS",
    "ensure_comments_schema",
]

COMMENTS_TABLE = "comments"
COMMENT_SYNC_STATE_TABLE = "comment_sync_state"
COMMENT_ANALYSES_TABLE = "comment_analyses"
COMMENT_DRAFTS_TABLE = "comment_drafts"
COMMENT_MODERATION_FLAGS_TABLE = "comment_moderation_flags"

# Kept byte-identical with migrations/versions/0006_comments.py (the
# migration carries the same constants under the same names).
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

#: All five table DDL statements, in creation (FK dependency) order.
COMMENTS_SCHEMA_DDL = (
    COMMENTS_DDL,
    COMMENT_SYNC_STATE_DDL,
    COMMENT_ANALYSES_DDL,
    COMMENT_DRAFTS_DDL,
    COMMENT_MODERATION_FLAGS_DDL,
)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_comments_channel_video "
    "ON comments (channel_id, video_id, published_at)",
    "CREATE INDEX IF NOT EXISTS idx_comments_channel_published "
    "ON comments (channel_id, published_at)",
    "CREATE INDEX IF NOT EXISTS idx_comment_sync_state_channel "
    "ON comment_sync_state (channel_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_comment_analyses_channel_video "
    "ON comment_analyses (channel_id, video_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_comment_analyses_channel_scope "
    "ON comment_analyses (channel_id, scope, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_comment_drafts_comment "
    "ON comment_drafts (comment_id)",
    "CREATE INDEX IF NOT EXISTS idx_comment_drafts_channel_status "
    "ON comment_drafts (channel_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_comment_moderation_flags_comment "
    "ON comment_moderation_flags (comment_id)",
    "CREATE INDEX IF NOT EXISTS idx_comment_moderation_flags_channel_status "
    "ON comment_moderation_flags (channel_id, status)",
)

# Alias following the budget_reservations naming pattern.
_INDEX_STATEMENTS = INDEX_STATEMENTS


def _commit_quietly(conn: Any) -> None:
    try:
        conn.commit()
    except Exception:
        pass


#: Columns of ``comment_drafts``, in DDL order (rebuild copy list).
_DRAFT_COLUMNS = (
    "id, comment_id, channel_id, video_id, draft_text, status,"
    " created_by, provenance_json, decided_at, decided_by,"
    " created_at, updated_at"
)


def _upgrade_drafts_status_vocabulary(conn: Any) -> None:
    """Widen a PRE-EXISTING ``comment_drafts`` table to know
    ``'discarded'`` (R5-004 closure).

    Tables created by an older build carry
    ``CHECK (status IN ('proposed', 'approved', 'rejected'))``;
    SQLite cannot alter a CHECK in place, so the table is rebuilt
    with the current DDL, copying every row verbatim (no row is
    dropped or re-stamped).  No-op when the table does not exist
    yet (the DDL below creates it current) or already knows the
    status.  NOTE: migration ``0006_comments`` still carries the
    old CHECK in its own DDL copy — aligning the migration chain
    is owned by the migration worker; this runtime upgrade keeps
    already-created app databases correct in the meantime.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table'"
        " AND name = 'comment_drafts'"
    ).fetchone()
    if row is None or not row[0]:
        return
    if "'discarded'" in str(row[0]):
        return
    new_ddl = COMMENT_DRAFTS_DDL.replace(
        "comment_drafts", "comment_drafts__r5_004", 1
    )
    conn.execute(new_ddl)
    conn.execute(
        f"INSERT INTO comment_drafts__r5_004 ({_DRAFT_COLUMNS})"
        f" SELECT {_DRAFT_COLUMNS} FROM comment_drafts"
    )
    conn.execute("DROP TABLE comment_drafts")
    conn.execute(
        "ALTER TABLE comment_drafts__r5_004 RENAME TO comment_drafts"
    )


def ensure_comments_schema(conn: Any) -> None:
    """Create the Comments tables + indexes on ``conn`` if absent.

    Idempotent (every statement is IF NOT EXISTS) and safe to call on
    every store / router entry point, mirroring
    :func:`app.style_lab.schema.ensure_style_lab_schema`.  Also
    upgrades a pre-existing ``comment_drafts`` table to the current
    status vocabulary (see :func:`_upgrade_drafts_status_vocabulary`).
    """
    _upgrade_drafts_status_vocabulary(conn)
    for statement in COMMENTS_SCHEMA_DDL:
        conn.execute(statement)
    for statement in INDEX_STATEMENTS:
        conn.execute(statement)
    _commit_quietly(conn)
