"""Schema tests for Round 5 analytics ingestion (migration 0007).

Covers the DB layer only (store behaviour is the sibling suite
``test_analytics_store.py``):

* migration ``0007_analytics`` metadata (revision / down_revision)
  and DDL kept in sync with :mod:`app.analytics.schema`;
* alembic upgrade from an empty DB: ``analytics_snapshots`` keeps
  ALL of its section-15 legacy columns (in order) plus the five
  Round 5 additions, ``channel_metric_snapshots`` is created with
  the contract columns, CHECKs, FKs and (unique) indexes; downgrade
  to ``0006`` removes the additions + the new table and leaves the
  base schema intact; re-upgrade is clean;
* :func:`app.analytics.schema.ensure_analytics_schema` is
  idempotent, evolves a legacy-shaped table in place (with
  backfill), and produces the same columns / FKs / indexes as the
  migration (raw SQL text is not compared for
  ``analytics_snapshots``: an ALTER-evolved table's stored CREATE
  text legitimately differs from a fresh CREATE);
* FKs to ``channels`` / ``projects`` are enforced (RESTRICT),
  CHECKs reject bad snapshot_kind values, and the day-uniqueness
  indexes reject exact (channel, video, kind, day) duplicates
  while allowing different days / kinds / NULL video ids.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.analytics import schema as analytics_schema

BACKEND_DIR = Path(__file__).resolve().parent.parent

NOW_MS = 1_700_000_000_000  # 2023-11-14T22:13:20Z
NOW_DAY = "2023-11-14"

#: Section-15 legacy columns, in their original order.
LEGACY_ANALYTICS_COLUMNS = [
    "id",
    "project_id",
    "channel_id",
    "youtube_video_id",
    "snapshot_at",
    "views",
    "watch_hours_x100",
    "likes",
    "comments_count",
    "shares",
    "ctr_bps",
    "avg_view_duration_sec",
    "retention_curve_json",
    "subs_delta",
    "revenue_micros",
    "source",
    "created_at",
]

ADDED_ANALYTICS_COLUMNS = [
    "snapshot_kind",
    "capture_day",
    "metrics_json",
    "captured_at",
    "updated_at",
]

EXPECTED_ANALYTICS_COLUMNS = LEGACY_ANALYTICS_COLUMNS + ADDED_ANALYTICS_COLUMNS

EXPECTED_CHANNEL_METRIC_COLUMNS = [
    "id",
    "channel_id",
    "snapshot_kind",
    "captured_at",
    "capture_day",
    "subscribers",
    "total_views",
    "watch_hours",
    "qualified_watch_hours",
    "metrics_json",
    "created_at",
    "updated_at",
]

EXPECTED_ANALYTICS_INDEXES = {
    "idx_snapshots_video",
    "idx_snapshots_channel",
    "uq_analytics_snapshots_kind_day",
    "idx_analytics_snapshots_project",
}

EXPECTED_CHANNEL_METRIC_INDEXES = {
    "uq_channel_metric_snapshots_kind_day",
    "idx_channel_metric_snapshots_channel_time",
}


def _load_migration_0007():
    path = BACKEND_DIR / "migrations" / "versions" / "0007_analytics.py"
    spec = importlib.util.spec_from_file_location("migration_0007", path)
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


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _structure(conn: sqlite3.Connection, table: str) -> dict:
    """Comparable structural fingerprint (columns / FKs / indexes)."""
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


def test_migration_0007_metadata() -> None:
    module = _load_migration_0007()
    assert module.revision == "0007"
    assert module.down_revision == "0006"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_ddl_in_sync_with_runtime_schema() -> None:
    module = _load_migration_0007()
    assert module.ANALYTICS_SNAPSHOTS_DDL == analytics_schema.ANALYTICS_SNAPSHOTS_DDL
    assert (
        module.CHANNEL_METRIC_SNAPSHOTS_DDL
        == analytics_schema.CHANNEL_METRIC_SNAPSHOTS_DDL
    )
    assert module.COLUMN_ADDITIONS == analytics_schema.COLUMN_ADDITIONS
    migration_source = (
        BACKEND_DIR / "migrations" / "versions" / "0007_analytics.py"
    ).read_text(encoding="utf-8")
    normalised_source = " ".join(migration_source.replace('"', " ").split())
    for statement in analytics_schema.INDEX_STATEMENTS:
        assert " ".join(statement.split()) in normalised_source


# ---------------------------------------------------------------------------
# Alembic upgrade from an empty DB / downgrade / re-upgrade
# ---------------------------------------------------------------------------


def _alembic_config(tmp_path: Path, monkeypatch, name: str = "migrated.db"):
    from alembic.config import Config

    db_file = tmp_path / name
    monkeypatch.setenv("YTA_DB_URL", f"sqlite:///{db_file}")
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return cfg, db_file


def test_migration_0007_upgrade_from_empty_and_downgrade(tmp_path, monkeypatch):
    from alembic import command

    cfg, db_file = _alembic_config(tmp_path, monkeypatch)
    # Pinned to this migration's own revision (not "head"), the
    # test_comments_schema pattern — later Round 5 migrations chain
    # on top of 0007 and must not move this test's target.
    command.upgrade(cfg, "0007")

    conn = sqlite3.connect(str(db_file))
    try:
        assert conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "0007"
        tables = _tables(conn)
        assert "analytics_snapshots" in tables
        assert "channel_metric_snapshots" in tables
        # Legacy columns preserved verbatim + Round 5 additions appended.
        assert _columns(conn, "analytics_snapshots") == EXPECTED_ANALYTICS_COLUMNS
        assert (
            _columns(conn, "channel_metric_snapshots")
            == EXPECTED_CHANNEL_METRIC_COLUMNS
        )
        indexes = _indexes(conn)
        assert EXPECTED_ANALYTICS_INDEXES <= indexes
        assert EXPECTED_CHANNEL_METRIC_INDEXES <= indexes

        analytics_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'analytics_snapshots'"
            ).fetchone()[0].split()
        )
        assert (
            "CHECK (snapshot_kind IN ('d3', 'd7', 'd28', 'd90', 'manual', "
            "'scheduled'))" in analytics_sql
        )
        channel_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE name = 'channel_metric_snapshots'"
            ).fetchone()[0].split()
        )
        assert (
            "CHECK (snapshot_kind IN ('scheduled', 'manual'))" in channel_sql
        )

        # FK targets + ON DELETE RESTRICT.
        assert _structure(conn, "analytics_snapshots")["foreign_keys"] == [
            ("channels", "channel_id", "id", "RESTRICT"),
            ("projects", "project_id", "id", "RESTRICT"),
        ]
        assert _structure(conn, "channel_metric_snapshots")[
            "foreign_keys"
        ] == [("channels", "channel_id", "id", "RESTRICT")]

        # The day-uniqueness indexes really are UNIQUE.
        for table, unique_name in (
            ("analytics_snapshots", "uq_analytics_snapshots_kind_day"),
            ("channel_metric_snapshots", "uq_channel_metric_snapshots_kind_day"),
        ):
            unique_flags = {
                row[1]: row[2]
                for row in conn.execute(f"PRAGMA index_list({table})")
            }
            assert unique_flags[unique_name] == 1
    finally:
        conn.close()

    command.downgrade(cfg, "0006")
    conn = sqlite3.connect(str(db_file))
    try:
        assert conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "0006"
        tables = _tables(conn)
        assert "channel_metric_snapshots" not in tables
        # The legacy table is back to exactly its section-15 shape.
        assert _columns(conn, "analytics_snapshots") == LEGACY_ANALYTICS_COLUMNS
        indexes = _indexes(conn)
        assert not (
            {
                "uq_analytics_snapshots_kind_day",
                "idx_analytics_snapshots_project",
            }
            & indexes
        )
        assert not (EXPECTED_CHANNEL_METRIC_INDEXES & indexes)
        # Legacy indexes + the base schema survive.  (No assertion
        # on sibling Round 5 tables: two teams both claimed revision
        # 0005 — learning_rules and reports — so which 0005 branch
        # alembic walked is an integration-pass concern, not this
        # migration's.)
        assert {"idx_snapshots_video", "idx_snapshots_channel"} <= indexes
        assert "channels" in tables
        assert "projects" in tables
    finally:
        conn.close()

    # Re-upgrade is clean (guarded ADD COLUMN path).
    command.upgrade(cfg, "0007")
    conn = sqlite3.connect(str(db_file))
    try:
        assert _columns(conn, "analytics_snapshots") == EXPECTED_ANALYTICS_COLUMNS
        assert "channel_metric_snapshots" in _tables(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ensure_analytics_schema: idempotent + matches the migration
# ---------------------------------------------------------------------------


def test_ensure_analytics_schema_idempotent_and_matches_migration(
    tmp_path, monkeypatch
):
    from alembic import command

    cfg, db_file = _alembic_config(tmp_path, monkeypatch, name="migrated2.db")
    command.upgrade(cfg, "0007")
    migrated = sqlite3.connect(str(db_file))
    try:
        ensured = sqlite3.connect(":memory:")
        try:
            analytics_schema.ensure_analytics_schema(ensured)
            analytics_schema.ensure_analytics_schema(ensured)
            analytics_schema.ensure_analytics_schema(ensured)

            # channel_metric_snapshots: full structural identity,
            # including the stored CREATE text (single shared DDL).
            assert _structure(ensured, "channel_metric_snapshots") == _structure(
                migrated, "channel_metric_snapshots"
            )
            # analytics_snapshots: columns / FKs / indexes identical
            # (stored CREATE text differs — evolved vs fresh).
            for key in ("columns", "foreign_keys", "indexes"):
                assert _structure(ensured, "analytics_snapshots")[key] == (
                    _structure(migrated, "analytics_snapshots")[key]
                )
            assert EXPECTED_ANALYTICS_INDEXES <= _indexes(ensured)
            assert EXPECTED_CHANNEL_METRIC_INDEXES <= _indexes(ensured)
        finally:
            ensured.close()
    finally:
        migrated.close()


def test_ensure_evolves_legacy_table_with_backfill() -> None:
    """A section-15-shaped table gains the new columns in order and
    its rows are backfilled (captured_at / updated_at / capture_day)."""
    from app.schema_ddl import DDL_STATEMENTS

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for statement in DDL_STATEMENTS:
            conn.execute(statement)
        assert _columns(conn, "analytics_snapshots") == LEGACY_ANALYTICS_COLUMNS
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, created_at,"
            " updated_at) VALUES ('n1', 'facts', 'Facts', 'builtin', ?, ?)",
            (NOW_MS, NOW_MS),
        )
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language,"
            " created_at, updated_at) VALUES ('chan-1', 'UCx', 'T', 'en', ?, ?)",
            (NOW_MS, NOW_MS),
        )
        conn.execute(
            "INSERT INTO projects (id, channel_id, niche_profile_id, topic,"
            " topic_source, created_at, updated_at) VALUES ('proj-1',"
            " 'chan-1', 'n1', 'Topic', 'user', ?, ?)",
            (NOW_MS, NOW_MS),
        )
        conn.execute(
            "INSERT INTO analytics_snapshots (id, project_id, channel_id,"
            " youtube_video_id, snapshot_at, views, created_at) VALUES"
            " ('snap-legacy', 'proj-1', 'chan-1', 'vid-1', ?, 42, ?)",
            (NOW_MS, NOW_MS),
        )
        conn.commit()

        analytics_schema.ensure_analytics_schema(conn)

        assert _columns(conn, "analytics_snapshots") == EXPECTED_ANALYTICS_COLUMNS
        row = conn.execute(
            "SELECT snapshot_kind, capture_day, captured_at, updated_at,"
            " metrics_json FROM analytics_snapshots WHERE id = 'snap-legacy'"
        ).fetchone()
        assert row == ("scheduled", NOW_DAY, NOW_MS, NOW_MS, None)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# FK enforcement + CHECK constraints + uniqueness (runtime schema)
# ---------------------------------------------------------------------------


def _conn_with_parents() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
    analytics_schema.ensure_analytics_schema(conn)
    conn.execute("INSERT INTO channels (id) VALUES ('chan-1')")
    conn.execute("INSERT INTO projects (id) VALUES ('proj-1')")
    conn.commit()
    return conn


def _insert_snapshot(conn: sqlite3.Connection, **overrides) -> str:
    row = {
        "id": uuid.uuid4().hex,
        "channel_id": "chan-1",
        "youtube_video_id": "vid-1",
        "snapshot_at": NOW_MS,
        "snapshot_kind": "d7",
        "capture_day": NOW_DAY,
        "captured_at": NOW_MS,
        "created_at": NOW_MS,
        "updated_at": NOW_MS,
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO analytics_snapshots ({cols}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    return row["id"]


def _insert_channel_metrics(conn: sqlite3.Connection, **overrides) -> str:
    row = {
        "id": uuid.uuid4().hex,
        "channel_id": "chan-1",
        "snapshot_kind": "scheduled",
        "captured_at": NOW_MS,
        "capture_day": NOW_DAY,
        "created_at": NOW_MS,
        "updated_at": NOW_MS,
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO channel_metric_snapshots ({cols}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    return row["id"]


def test_fk_enforced_and_restrict() -> None:
    conn = _conn_with_parents()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot(conn, channel_id="bogus-channel")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot(conn, project_id="bogus-project")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_channel_metrics(conn, channel_id="bogus-channel")
        conn.rollback()

        _insert_snapshot(conn, project_id="proj-1")
        _insert_channel_metrics(conn)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM channels WHERE id = 'chan-1'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM projects WHERE id = 'proj-1'")
        conn.rollback()
    finally:
        conn.close()


def test_snapshot_kind_check_constraints() -> None:
    conn = _conn_with_parents()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot(conn, snapshot_kind="d1")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_channel_metrics(conn, snapshot_kind="d7")
        conn.rollback()
        # Contract defaults apply.
        row_id = _insert_channel_metrics(
            conn, id="cm-default", snapshot_kind="manual"
        )
        conn.commit()
        row = conn.execute(
            "SELECT snapshot_kind, subscribers, watch_hours,"
            " qualified_watch_hours FROM channel_metric_snapshots"
            " WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row == ("manual", None, None, None)
    finally:
        conn.close()


def test_day_uniqueness_enforced() -> None:
    conn = _conn_with_parents()
    try:
        _insert_snapshot(conn)
        conn.commit()
        # Exact (channel, video, kind, day) duplicate is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot(conn)
        conn.rollback()
        # A different day, kind or channel is a different snapshot.
        _insert_snapshot(conn, capture_day="2023-11-15")
        _insert_snapshot(conn, snapshot_kind="d28")
        conn.execute("INSERT INTO channels (id) VALUES ('chan-2')")
        _insert_snapshot(conn, channel_id="chan-2")
        conn.commit()
        # NULL video ids are exempt from the uniqueness.
        _insert_snapshot(conn, youtube_video_id=None)
        _insert_snapshot(conn, youtube_video_id=None)
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM analytics_snapshots"
        ).fetchone()[0] == 6

        _insert_channel_metrics(conn)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_channel_metrics(conn)
        conn.rollback()
        _insert_channel_metrics(conn, snapshot_kind="manual")
        _insert_channel_metrics(conn, capture_day="2023-11-15")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM channel_metric_snapshots"
        ).fetchone()[0] == 3
    finally:
        conn.close()
