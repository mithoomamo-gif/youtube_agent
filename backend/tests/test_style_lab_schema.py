"""Schema tests for the Style Lab mini-round (migration 0004 + runtime DDL).

Covers the DB layer only (intake parsing / store / service / router
are sibling suites):

* migration ``0004_style_lab`` metadata (revision / down_revision)
  and DDL kept in sync with :mod:`app.style_lab.schema`;
* alembic upgrade from an empty DB creates all three tables with the
  contract columns, CHECKs, FKs and indexes; downgrade to ``0003``
  removes the tables + indexes and leaves the base schema intact;
* :func:`app.style_lab.schema.ensure_style_lab_schema` is idempotent
  and produces a structure identical to the migrated one;
* FOREIGN KEYs to ``channels`` (and to ``competitor_channels``) are
  enforced with ``PRAGMA foreign_keys = ON``, including
  ON DELETE RESTRICT;
* CHECK constraints reject bad platform / source_kind /
  metadata_status / status / source / analysis_status values, and
  the contract defaults are applied on insert.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.style_lab import schema as style_schema

BACKEND_DIR = Path(__file__).resolve().parent.parent

NOW_MS = 1_700_000_000_000

EXPECTED_COLUMNS = {
    "competitor_channels": [
        "id",
        "channel_id",
        "platform",
        "source_input",
        "source_kind",
        "external_channel_id",
        "handle",
        "display_name",
        "channel_url",
        "metadata_status",
        "metadata_json",
        "status",
        "created_at",
        "updated_at",
    ],
    "competitor_videos": [
        "id",
        "competitor_channel_id",
        "source",
        "external_video_id",
        "video_url",
        "title",
        "description",
        "tags_json",
        "storage_path",
        "file_sha256",
        "file_size_bytes",
        "mime_type",
        "duration_s",
        "width",
        "height",
        "thumbnail_storage_path",
        "thumbnail_sha256",
        "analysis_status",
        "analysis_requested_at",
        "created_at",
        "updated_at",
    ],
    "style_profiles": [
        "id",
        "channel_id",
        "name",
        "version",
        "status",
        "competitor_ids_json",
        "profile_json",
        "created_at",
        "updated_at",
    ],
}

EXPECTED_INDEXES = {
    "idx_competitor_channels_channel_status",
    "idx_competitor_videos_channel",
    "idx_competitor_videos_analysis_status",
    "idx_style_profiles_channel_status",
}


def _load_migration_0004():
    path = BACKEND_DIR / "migrations" / "versions" / "0004_style_lab.py"
    spec = importlib.util.spec_from_file_location("migration_0004", path)
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
    """Comparable structural fingerprint for one table."""
    columns = [
        tuple(row)
        for row in conn.execute(f"PRAGMA table_info({table})")
    ]
    foreign_keys = sorted(
        (row[2], row[3], row[4], row[6])  # table, from, to, on_delete
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


def test_migration_0004_metadata() -> None:
    module = _load_migration_0004()
    assert module.revision == "0004"
    assert module.down_revision == "0003"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_ddl_in_sync_with_runtime_schema() -> None:
    module = _load_migration_0004()
    assert module.COMPETITOR_CHANNELS_DDL == style_schema.COMPETITOR_CHANNELS_DDL
    assert module.COMPETITOR_VIDEOS_DDL == style_schema.COMPETITOR_VIDEOS_DDL
    assert module.STYLE_PROFILES_DDL == style_schema.STYLE_PROFILES_DDL
    # Every runtime index statement is also executed by the migration
    # (the migration splits each statement across two string literals,
    # so compare with quotes / whitespace normalised away).
    migration_source = (
        BACKEND_DIR / "migrations" / "versions" / "0004_style_lab.py"
    ).read_text(encoding="utf-8")
    normalised_source = " ".join(migration_source.replace('"', " ").split())
    for statement in style_schema.INDEX_STATEMENTS:
        assert " ".join(statement.split()) in normalised_source


# ---------------------------------------------------------------------------
# Alembic upgrade from an empty DB / downgrade
# ---------------------------------------------------------------------------


def _alembic_config(tmp_path: Path, monkeypatch, name: str = "migrated.db"):
    from alembic.config import Config

    db_file = tmp_path / name
    monkeypatch.setenv("YTA_DB_URL", f"sqlite:///{db_file}")
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return cfg, db_file


def test_migration_0004_upgrade_from_empty_and_downgrade(tmp_path, monkeypatch):
    from alembic import command

    cfg, db_file = _alembic_config(tmp_path, monkeypatch)
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_file))
    try:
        # Head moved past 0004 with the Round 5 migrations
        # (0005 learning_rules, 0006 comments, 0007 analytics,
        # 0008 channel_quality_tier, 0009 calibration_log,
        # 0010 reports, 0011 compliance).
        assert conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "0011"
        tables = _tables(conn)
        for table, expected_cols in EXPECTED_COLUMNS.items():
            assert table in tables
            cols = [
                row[1] for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            assert cols == expected_cols
        assert EXPECTED_INDEXES <= _indexes(conn)

        # CHECK clauses are present in the stored DDL.
        channels_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'competitor_channels'"
            ).fetchone()[0].split()
        )
        assert "CHECK (platform IN ('youtube'))" in channels_sql
        assert (
            "CHECK (source_kind IN ('channel_url', 'handle', 'channel_id', "
            "'video_url', 'custom_url'))" in channels_sql
        )
        assert (
            "CHECK (metadata_status IN ('not_fetched', 'fetched', "
            "'metadata_unavailable', 'fetch_failed'))" in channels_sql
        )
        assert "CHECK (status IN ('active', 'archived'))" in channels_sql

        videos_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'competitor_videos'"
            ).fetchone()[0].split()
        )
        assert (
            "CHECK (source IN ('attached_file', 'url_metadata', "
            "'pasted_metadata'))" in videos_sql
        )
        assert (
            "CHECK (analysis_status IN ('not_queued', 'queued', 'analysing', "
            "'analysed', 'analysis_failed'))" in videos_sql
        )

        profiles_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'style_profiles'"
            ).fetchone()[0].split()
        )
        assert (
            "CHECK (status IN ('draft', 'active', 'superseded'))"
            in profiles_sql
        )

        # FK targets + ON DELETE RESTRICT.
        assert _structure(conn, "competitor_channels")["foreign_keys"] == [
            ("channels", "channel_id", "id", "RESTRICT")
        ]
        assert _structure(conn, "competitor_videos")["foreign_keys"] == [
            ("competitor_channels", "competitor_channel_id", "id", "RESTRICT")
        ]
        assert _structure(conn, "style_profiles")["foreign_keys"] == [
            ("channels", "channel_id", "id", "RESTRICT")
        ]
    finally:
        conn.close()

    command.downgrade(cfg, "0003")
    conn = sqlite3.connect(str(db_file))
    try:
        assert conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "0003"
        tables = _tables(conn)
        for table in EXPECTED_COLUMNS:
            assert table not in tables
        assert not (EXPECTED_INDEXES & _indexes(conn))
        # Base schema survives the downgrade.
        assert "channels" in tables
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ensure_style_lab_schema: idempotent + identical to the migration
# ---------------------------------------------------------------------------


def test_ensure_style_lab_schema_idempotent_and_matches_migration(
    tmp_path, monkeypatch
):
    from alembic import command

    cfg, db_file = _alembic_config(tmp_path, monkeypatch, name="migrated2.db")
    command.upgrade(cfg, "head")
    migrated = sqlite3.connect(str(db_file))
    try:
        ensured = sqlite3.connect(":memory:")
        try:
            style_schema.ensure_style_lab_schema(ensured)
            # Idempotent: a second (and third) call changes nothing.
            style_schema.ensure_style_lab_schema(ensured)
            style_schema.ensure_style_lab_schema(ensured)
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
    """In-memory DB: minimal ``channels`` parent + Style Lab schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
    style_schema.ensure_style_lab_schema(conn)
    conn.execute("INSERT INTO channels (id) VALUES ('chan-1')")
    conn.commit()
    return conn


def _insert_competitor(conn: sqlite3.Connection, **overrides) -> str:
    row = {
        "id": uuid.uuid4().hex,
        "channel_id": "chan-1",
        "source_input": "@SomeHandle",
        "source_kind": "handle",
        "created_at": NOW_MS,
        "updated_at": NOW_MS,
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO competitor_channels ({cols}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    return row["id"]


def _insert_video(conn: sqlite3.Connection, competitor_id: str, **overrides) -> str:
    row = {
        "id": uuid.uuid4().hex,
        "competitor_channel_id": competitor_id,
        "source": "url_metadata",
        "created_at": NOW_MS,
        "updated_at": NOW_MS,
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO competitor_videos ({cols}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    return row["id"]


def _insert_profile(conn: sqlite3.Connection, **overrides) -> str:
    row = {
        "id": uuid.uuid4().hex,
        "channel_id": "chan-1",
        "name": "Profile",
        "created_at": NOW_MS,
        "updated_at": NOW_MS,
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO style_profiles ({cols}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    return row["id"]


def test_fk_to_channels_enforced() -> None:
    conn = _conn_with_channel()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_competitor(conn, channel_id="bogus-channel")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_profile(conn, channel_id="bogus-channel")
        conn.rollback()
        # …and to competitor_channels for videos.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_video(conn, "bogus-competitor")
        conn.rollback()
    finally:
        conn.close()


def test_fk_on_delete_restrict() -> None:
    conn = _conn_with_channel()
    try:
        competitor_id = _insert_competitor(conn)
        _insert_video(conn, competitor_id)
        _insert_profile(conn)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM channels WHERE id = 'chan-1'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM competitor_channels WHERE id = ?", (competitor_id,)
            )
        conn.rollback()
    finally:
        conn.close()


def test_defaults_applied_on_insert() -> None:
    conn = _conn_with_channel()
    try:
        competitor_id = _insert_competitor(conn)
        video_id = _insert_video(conn, competitor_id)
        profile_id = _insert_profile(conn)
        conn.commit()
        row = conn.execute(
            "SELECT platform, metadata_status, status FROM competitor_channels "
            "WHERE id = ?",
            (competitor_id,),
        ).fetchone()
        assert row == ("youtube", "not_fetched", "active")
        row = conn.execute(
            "SELECT analysis_status, analysis_requested_at "
            "FROM competitor_videos WHERE id = ?",
            (video_id,),
        ).fetchone()
        assert row == ("not_queued", None)
        row = conn.execute(
            "SELECT version, status, competitor_ids_json FROM style_profiles "
            "WHERE id = ?",
            (profile_id,),
        ).fetchone()
        assert row == (1, "draft", "[]")
    finally:
        conn.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"platform": "vimeo"},
        {"source_kind": "bogus"},
        {"metadata_status": "bogus"},
        {"status": "bogus"},
    ],
)
def test_competitor_channels_check_constraints(overrides) -> None:
    conn = _conn_with_channel()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_competitor(conn, **overrides)
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "bogus"},
        {"analysis_status": "bogus"},
    ],
)
def test_competitor_videos_check_constraints(overrides) -> None:
    conn = _conn_with_channel()
    try:
        competitor_id = _insert_competitor(conn)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_video(conn, competitor_id, **overrides)
        conn.rollback()
    finally:
        conn.close()


def test_style_profiles_check_constraints() -> None:
    conn = _conn_with_channel()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_profile(conn, status="bogus")
        conn.rollback()
    finally:
        conn.close()
