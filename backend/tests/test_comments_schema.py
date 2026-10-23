"""Schema tests for Round 5 Comments Intelligence (migration 0006).

Covers the DB layer only (store is a sibling suite):

* migration ``0006_comments`` metadata (revision / down_revision —
  chained onto the analytics/learning team's ``0005``) and DDL kept
  byte-identical with :mod:`app.comments.schema`;
* alembic upgrade to ``0006`` creates all five tables with the
  contract columns, CHECKs, FKs and indexes; downgrade to ``0005``
  removes them and leaves the base schema intact;
* :func:`app.comments.schema.ensure_comments_schema` is idempotent
  and produces a structure identical to the migrated one (lazy-DB
  parity);
* FOREIGN KEYs are enforced, including ON DELETE RESTRICT (channels)
  and ON DELETE CASCADE (comment -> drafts / flags);
* CHECK constraints reject bad status / scope / engine / kind values
  and the contract defaults are applied on insert.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.comments import schema as comments_schema

BACKEND_DIR = Path(__file__).resolve().parent.parent

NOW_MS = 1_700_000_000_000

EXPECTED_COLUMNS = {
    "comments": [
        "id", "channel_id", "video_id", "project_id",
        "author_display_name", "author_channel_id", "text",
        "like_count", "reply_count", "parent_comment_id", "is_top_level",
        "published_at", "fetched_at", "raw_json", "created_at",
        "updated_at",
    ],
    "comment_sync_state": [
        "channel_id", "video_id", "project_id", "status",
        "last_synced_at", "comment_count", "error_reason", "created_at",
        "updated_at",
    ],
    "comment_analyses": [
        "id", "channel_id", "scope", "video_id", "rollup_json", "engine",
        "model_id", "comment_count", "provenance_json", "created_at",
        "updated_at",
    ],
    "comment_drafts": [
        "id", "comment_id", "channel_id", "video_id", "draft_text",
        "status", "created_by", "provenance_json", "decided_at",
        "decided_by", "created_at", "updated_at",
    ],
    "comment_moderation_flags": [
        "id", "comment_id", "channel_id", "video_id", "flag_kind",
        "reason", "suggested_action", "status", "provenance_json",
        "decided_at", "decided_by", "created_at", "updated_at",
    ],
}

EXPECTED_INDEXES = {
    "idx_comments_channel_video",
    "idx_comments_channel_published",
    "idx_comment_sync_state_channel",
    "idx_comment_analyses_channel_video",
    "idx_comment_analyses_channel_scope",
    "idx_comment_drafts_comment",
    "idx_comment_drafts_channel_status",
    "idx_comment_moderation_flags_comment",
    "idx_comment_moderation_flags_channel_status",
}

DDL_PAIRS = [
    ("COMMENTS_DDL", "COMMENTS_DDL"),
    ("COMMENT_SYNC_STATE_DDL", "COMMENT_SYNC_STATE_DDL"),
    ("COMMENT_ANALYSES_DDL", "COMMENT_ANALYSES_DDL"),
    ("COMMENT_DRAFTS_DDL", "COMMENT_DRAFTS_DDL"),
    ("COMMENT_MODERATION_FLAGS_DDL", "COMMENT_MODERATION_FLAGS_DDL"),
]


def _load_migration_0006():
    path = BACKEND_DIR / "migrations" / "versions" / "0006_comments.py"
    spec = importlib.util.spec_from_file_location("migration_0006", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }


def _structure(conn: sqlite3.Connection, table: str) -> dict:
    columns = [tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")]
    foreign_keys = sorted(
        (row[2], row[3], row[4], row[6])
        for row in conn.execute(f"PRAGMA foreign_key_list({table})")
    )
    indexes = {}
    for row in conn.execute(f"PRAGMA index_list({table})"):
        name = row[1]
        indexes[name] = [
            info[2] for info in conn.execute(f"PRAGMA index_info({name})")
        ]
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return {
        "columns": columns,
        "foreign_keys": foreign_keys,
        "indexes": indexes,
        "sql": " ".join(sql_row[0].split()) if sql_row else None,
    }


# ---------------------------------------------------------------------------
# Migration metadata + DDL sync
# ---------------------------------------------------------------------------


def test_migration_0006_metadata() -> None:
    module = _load_migration_0006()
    assert module.revision == "0006"
    # Chained onto the analytics/learning team's 0005 (present in tree).
    assert module.down_revision == "0005"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_ddl_byte_identical_with_runtime_schema() -> None:
    module = _load_migration_0006()
    for migration_name, schema_name in DDL_PAIRS:
        assert getattr(module, migration_name) == getattr(
            comments_schema, schema_name
        )
    migration_source = (
        BACKEND_DIR / "migrations" / "versions" / "0006_comments.py"
    ).read_text(encoding="utf-8")
    normalised_source = " ".join(migration_source.replace('"', " ").split())
    for statement in comments_schema.INDEX_STATEMENTS:
        assert " ".join(statement.split()) in normalised_source


# ---------------------------------------------------------------------------
# Alembic upgrade / downgrade
# ---------------------------------------------------------------------------


def _alembic_config(tmp_path: Path, monkeypatch, name: str = "migrated.db"):
    from alembic.config import Config

    db_file = tmp_path / name
    monkeypatch.setenv("YTA_DB_URL", f"sqlite:///{db_file}")
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return cfg, db_file


def test_migration_0006_upgrade_and_downgrade(tmp_path, monkeypatch):
    from alembic import command

    cfg, db_file = _alembic_config(tmp_path, monkeypatch)
    command.upgrade(cfg, "0006")

    conn = sqlite3.connect(str(db_file))
    try:
        assert conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "0006"
        tables = _tables(conn)
        for table, expected_cols in EXPECTED_COLUMNS.items():
            assert table in tables
            cols = [
                row[1] for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            assert cols == expected_cols
        assert EXPECTED_INDEXES <= _indexes(conn)

        comments_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'comments'"
            ).fetchone()[0].split()
        )
        assert "CHECK (is_top_level IN (0, 1))" in comments_sql

        sync_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'comment_sync_state'"
            ).fetchone()[0].split()
        )
        assert (
            "CHECK (status IN ('never_synced', 'synced', 'not_connected',"
            " 'error'))" in sync_sql
        )
        assert "PRIMARY KEY (channel_id, video_id)" in sync_sql

        analyses_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'comment_analyses'"
            ).fetchone()[0].split()
        )
        assert "CHECK (engine IN ('llm', 'deterministic'))" in analyses_sql
        assert "CHECK (scope IN ('video', 'channel'))" in analyses_sql

        drafts_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'comment_drafts'"
            ).fetchone()[0].split()
        )
        assert (
            "CHECK (status IN ('proposed', 'approved', 'rejected',"
            " 'discarded'))" in drafts_sql
        )

        flags_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master"
                " WHERE name = 'comment_moderation_flags'"
            ).fetchone()[0].split()
        )
        assert "CHECK (flag_kind IN ('spam', 'abuse', 'other'))" in flags_sql
        assert (
            "CHECK (status IN ('pending', 'dismissed', 'actioned'))"
            in flags_sql
        )

        assert _structure(conn, "comments")["foreign_keys"] == sorted([
            ("channels", "channel_id", "id", "RESTRICT"),
            ("projects", "project_id", "id", "SET NULL"),
        ])
        assert _structure(conn, "comment_drafts")["foreign_keys"] == sorted([
            ("comments", "comment_id", "id", "CASCADE"),
            ("channels", "channel_id", "id", "RESTRICT"),
        ])
        assert _structure(
            conn, "comment_moderation_flags"
        )["foreign_keys"] == sorted([
            ("comments", "comment_id", "id", "CASCADE"),
            ("channels", "channel_id", "id", "RESTRICT"),
        ])
    finally:
        conn.close()

    command.downgrade(cfg, "0005")
    conn = sqlite3.connect(str(db_file))
    try:
        assert conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "0005"
        tables = _tables(conn)
        for table in EXPECTED_COLUMNS:
            assert table not in tables
        assert not (EXPECTED_INDEXES & _indexes(conn))
        # Base schema survives the downgrade.
        assert "channels" in tables
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ensure_comments_schema: idempotent + identical to the migration
# ---------------------------------------------------------------------------


def test_ensure_comments_schema_idempotent_and_matches_migration(
    tmp_path, monkeypatch
):
    from alembic import command

    cfg, db_file = _alembic_config(tmp_path, monkeypatch, name="migrated2.db")
    command.upgrade(cfg, "0006")
    migrated = sqlite3.connect(str(db_file))
    try:
        ensured = sqlite3.connect(":memory:")
        try:
            # Parent tables must exist for structure parity of FK lists.
            ensured.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
            ensured.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
            comments_schema.ensure_comments_schema(ensured)
            comments_schema.ensure_comments_schema(ensured)
            comments_schema.ensure_comments_schema(ensured)
            for table in EXPECTED_COLUMNS:
                assert _structure(ensured, table) == _structure(migrated, table)
            assert _tables(ensured) >= set(EXPECTED_COLUMNS)
            assert EXPECTED_INDEXES <= _indexes(ensured)
        finally:
            ensured.close()
    finally:
        migrated.close()


# ---------------------------------------------------------------------------
# FK enforcement + CHECK constraints + defaults (runtime schema)
# ---------------------------------------------------------------------------


def _conn_with_channel() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
    comments_schema.ensure_comments_schema(conn)
    conn.execute("INSERT INTO channels (id) VALUES ('chan-1')")
    conn.commit()
    return conn


def _insert_comment(conn: sqlite3.Connection, **overrides) -> str:
    row = {
        "id": f"comment-{uuid.uuid4().hex[:12]}",
        "channel_id": "chan-1",
        "video_id": "video-1",
        "text": "hello",
        "fetched_at": NOW_MS,
        "created_at": NOW_MS,
        "updated_at": NOW_MS,
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO comments ({cols}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    return row["id"]


def test_fk_to_channels_enforced() -> None:
    conn = _conn_with_channel()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_comment(conn, channel_id="bogus-channel")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO comment_sync_state (channel_id, video_id,"
                " created_at, updated_at) VALUES ('bogus', 'v', ?, ?)",
                (NOW_MS, NOW_MS),
            )
        conn.rollback()
        # Draft / flag for a comment that does not exist (FK CASCADE parent).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO comment_drafts (id, comment_id, channel_id,"
                " video_id, draft_text, created_at, updated_at) VALUES"
                " ('d1', 'bogus-comment', 'chan-1', 'v', 'hi', ?, ?)",
                (NOW_MS, NOW_MS),
            )
        conn.rollback()
    finally:
        conn.close()


def test_fk_on_delete_restrict_and_cascade() -> None:
    conn = _conn_with_channel()
    try:
        comment_id = _insert_comment(conn)
        conn.execute(
            "INSERT INTO comment_drafts (id, comment_id, channel_id,"
            " video_id, draft_text, created_at, updated_at) VALUES"
            " ('d1', ?, 'chan-1', 'video-1', 'hi', ?, ?)",
            (comment_id, NOW_MS, NOW_MS),
        )
        conn.execute(
            "INSERT INTO comment_moderation_flags (id, comment_id,"
            " channel_id, video_id, flag_kind, created_at, updated_at)"
            " VALUES ('f1', ?, 'chan-1', 'video-1', 'spam', ?, ?)",
            (comment_id, NOW_MS, NOW_MS),
        )
        conn.commit()
        # Channel delete is RESTRICTed while comments exist.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM channels WHERE id = 'chan-1'")
        conn.rollback()
        # Deleting the comment cascades to drafts + flags.
        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM comment_drafts").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM comment_moderation_flags"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_defaults_applied_on_insert() -> None:
    conn = _conn_with_channel()
    try:
        comment_id = _insert_comment(conn)
        conn.execute(
            "INSERT INTO comment_sync_state (channel_id, video_id,"
            " created_at, updated_at) VALUES ('chan-1', 'video-1', ?, ?)",
            (NOW_MS, NOW_MS),
        )
        conn.execute(
            "INSERT INTO comment_drafts (id, comment_id, channel_id,"
            " video_id, draft_text, created_at, updated_at) VALUES"
            " ('d1', ?, 'chan-1', 'video-1', 'hi', ?, ?)",
            (comment_id, NOW_MS, NOW_MS),
        )
        conn.execute(
            "INSERT INTO comment_moderation_flags (id, comment_id,"
            " channel_id, video_id, created_at, updated_at)"
            " VALUES ('f1', ?, 'chan-1', 'video-1', ?, ?)",
            (comment_id, NOW_MS, NOW_MS),
        )
        conn.commit()
        row = conn.execute(
            "SELECT like_count, reply_count, is_top_level FROM comments"
            " WHERE id = ?",
            (comment_id,),
        ).fetchone()
        assert row == (0, 0, 1)
        row = conn.execute(
            "SELECT status, comment_count FROM comment_sync_state"
        ).fetchone()
        assert row == ("never_synced", 0)
        row = conn.execute(
            "SELECT status, created_by FROM comment_drafts"
        ).fetchone()
        assert row == ("proposed", "agent28")
        row = conn.execute(
            "SELECT status, flag_kind FROM comment_moderation_flags"
        ).fetchone()
        # flag_kind defaults to 'other' so kind-less writers (e.g. the
        # analysis engine's spam flags) still land honestly.
        assert row == ("pending", "other")
    finally:
        conn.close()


@pytest.mark.parametrize(
    "table,sql,params",
    [
        (
            "comments",
            "INSERT INTO comments (id, channel_id, video_id, text,"
            " is_top_level, fetched_at, created_at, updated_at) VALUES"
            " ('c-bad', 'chan-1', 'v', 'x', 5, ?, ?, ?)",
            (NOW_MS, NOW_MS, NOW_MS),
        ),
        (
            "comment_sync_state",
            "INSERT INTO comment_sync_state (channel_id, video_id, status,"
            " created_at, updated_at) VALUES ('chan-1', 'v', 'bogus', ?, ?)",
            (NOW_MS, NOW_MS),
        ),
        (
            "comment_analyses",
            "INSERT INTO comment_analyses (id, channel_id, engine,"
            " created_at, updated_at) VALUES ('a1', 'chan-1', 'bogus',"
            " ?, ?)",
            (NOW_MS, NOW_MS),
        ),
        (
            "comment_analyses",
            "INSERT INTO comment_analyses (id, channel_id, scope, engine,"
            " created_at, updated_at) VALUES ('a2', 'chan-1', 'video',"
            " 'llm', ?, ?)",  # video scope without video_id
            (NOW_MS, NOW_MS),
        ),
        (
            "comment_drafts",
            "INSERT INTO comment_drafts (id, comment_id, channel_id,"
            " video_id, draft_text, status, created_at, updated_at)"
            " VALUES ('d-bad', 'c-any', 'chan-1', 'v', 'x', 'bogus', ?, ?)",
            (NOW_MS, NOW_MS),
        ),
        (
            "comment_moderation_flags",
            "INSERT INTO comment_moderation_flags (id, comment_id,"
            " channel_id, video_id, flag_kind, created_at, updated_at)"
            " VALUES ('f-bad', 'c-any', 'chan-1', 'v', 'bogus', ?, ?)",
            (NOW_MS, NOW_MS),
        ),
    ],
)
def test_check_constraints(table, sql, params) -> None:
    conn = _conn_with_channel()
    try:
        # Drafts/flags reference a comment id; insert the parent first so
        # the failure under test is the CHECK, not the FK, where possible.
        if table in ("comment_drafts", "comment_moderation_flags"):
            _insert_comment(conn, id="c-any")
            conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, params)
        conn.rollback()
    finally:
        conn.close()
