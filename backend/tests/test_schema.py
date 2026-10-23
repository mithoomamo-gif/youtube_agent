"""Schema tests: init_db, pragmas, credential secrecy, append-only
provenance, and the hard UNIQUE constraints from section 15."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from app.db import init_db, schema_version, transaction
from app.schema_ddl import SCHEMA_VERSION, TABLE_NAMES

NOW_MS = 1_700_000_000_000

EXPECTED_TABLES = {
    "users",
    "settings",
    "provider_credentials",
    "channels",
    "niche_profiles",
    "projects",
    "project_artefacts",
    "jobs",
    "agent_runs",
    "pipeline_checkpoints",
    "assets",
    "asset_licences",
    "project_assets",
    "api_usage",
    "schedules",
    "schedule_runs",
    "topic_queue",
    "analytics_snapshots",
    "performance_insights",
    "used_topics",
    "pronunciation_dictionary",
    "cross_video_similarity",
    "provenance_log",
    "publish_records",
    "notifications",
    "event_log",
    "schema_meta",
}


@pytest.fixture()
def conn(tmp_path):
    connection = init_db(tmp_path / "app.db")
    yield connection
    connection.close()


def _tables(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(conn, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _seed_project(conn) -> tuple[str, str, str]:
    """Insert niche + channel + project; return (niche_id, channel_id, project_id)."""
    niche_id, channel_id, project_id = (uuid.uuid4().hex for _ in range(3))
    conn.execute(
        "INSERT INTO niche_profiles (id, key, label, source, created_at, updated_at)"
        " VALUES (?, 'facts', 'Facts', 'builtin', ?, ?)",
        (niche_id, NOW_MS, NOW_MS),
    )
    conn.execute(
        "INSERT INTO channels (id, youtube_channel_id, title, language,"
        " created_at, updated_at) VALUES (?, 'UCtest123', 'Test', 'en', ?, ?)",
        (channel_id, NOW_MS, NOW_MS),
    )
    conn.execute(
        "INSERT INTO projects (id, channel_id, niche_profile_id, topic,"
        " topic_source, created_at, updated_at)"
        " VALUES (?, ?, ?, 'A topic', 'user', ?, ?)",
        (project_id, channel_id, niche_id, NOW_MS, NOW_MS),
    )
    return niche_id, channel_id, project_id


def test_all_tables_exist(conn):
    tables = _tables(conn)
    assert EXPECTED_TABLES <= tables
    assert len(tables) >= 25
    assert set(TABLE_NAMES) == EXPECTED_TABLES


def test_schema_version_recorded(conn):
    assert schema_version(conn) == SCHEMA_VERSION


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "nested" / "app.db"
    first = init_db(path)
    first.close()
    second = init_db(path)  # parent dir creation + re-run must not fail
    assert EXPECTED_TABLES <= _tables(second)
    second.close()


def test_pragmas_applied(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA cache_size").fetchone()[0] == -64000


def test_provider_credentials_has_no_secret_column(conn):
    cols = _columns(conn, "provider_credentials")
    assert "keyring_ref" in cols
    for col in cols:
        lowered = col.lower()
        assert "secret" not in lowered
        assert "key_value" not in lowered
        assert "token" not in lowered
        assert "password" not in lowered
        assert lowered not in {"api_key", "value", "credential", "private_key"}


def test_key_indexes_exist(conn):
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    for name in (
        "idx_jobs_claim",
        "idx_jobs_depends",
        "idx_jobs_lease",
        "idx_jobs_status_time",
        "idx_artefacts_current",
        "idx_artefacts_locked",
        "idx_provenance_project",
        "idx_schedules_next",
    ):
        assert name in indexes


def test_provenance_log_trigger_blocks_update_and_delete(conn):
    _, _, project_id = _seed_project(conn)
    row_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO provenance_log (id, project_id, event_type, detail_json,"
        " created_at) VALUES (?, ?, 'research_source', '{}', ?)",
        (row_id, project_id, NOW_MS),
    )
    with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError)):
        conn.execute(
            "UPDATE provenance_log SET detail_json = '{\"tampered\": true}'"
            " WHERE id = ?",
            (row_id,),
        )
    with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError)):
        conn.execute("DELETE FROM provenance_log WHERE id = ?", (row_id,))
    # The original row is untouched.
    assert conn.execute(
        "SELECT detail_json FROM provenance_log WHERE id = ?", (row_id,)
    ).fetchone()[0] == "{}"


def test_used_topics_unique_per_channel(conn):
    _, channel_id, project_id = _seed_project(conn)
    insert = (
        "INSERT INTO used_topics (id, channel_id, topic_normalized,"
        " topic_hash, project_id, used_at) VALUES (?, ?, 'a topic',"
        " 'hash-abc', ?, ?)"
    )
    conn.execute(insert, (uuid.uuid4().hex, channel_id, project_id, NOW_MS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (uuid.uuid4().hex, channel_id, project_id, NOW_MS))


def test_jobs_idempotency_key_unique(conn):
    insert = (
        "INSERT INTO jobs (id, agent_key, kind, idempotency_key, created_at,"
        " updated_at) VALUES (?, 'upload', 'upload', 'upload-key-1', ?, ?)"
    )
    conn.execute(insert, (uuid.uuid4().hex, NOW_MS, NOW_MS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (uuid.uuid4().hex, NOW_MS, NOW_MS))
    # NULL idempotency keys (global jobs) must not collide.
    null_insert = (
        "INSERT INTO jobs (id, agent_key, kind, created_at, updated_at)"
        " VALUES (?, 'analytics', 'sweep', ?, ?)"
    )
    conn.execute(null_insert, (uuid.uuid4().hex, NOW_MS, NOW_MS))
    conn.execute(null_insert, (uuid.uuid4().hex, NOW_MS, NOW_MS))


def test_assets_content_hash_unique(conn):
    insert = (
        "INSERT INTO assets (id, content_hash, asset_type, file_path,"
        " source_provider, created_at) VALUES (?, 'hash-1', 'stock_video',"
        " 'lib/a.mp4', 'pexels', ?)"
    )
    conn.execute(insert, (uuid.uuid4().hex, NOW_MS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (uuid.uuid4().hex, NOW_MS))


def test_niche_other_requires_and_keeps_user_idea(conn):
    idea = "Stories of truck drivers in northern Pakistan — my own idea!"
    conn.execute(
        "INSERT INTO niche_profiles (id, key, label, source, user_idea,"
        " created_at, updated_at) VALUES (?, 'truck-stories', 'Truck Stories',"
        " 'other', ?, ?, ?)",
        (uuid.uuid4().hex, idea, NOW_MS, NOW_MS),
    )
    row = conn.execute(
        "SELECT source, user_idea FROM niche_profiles WHERE key = 'truck-stories'"
    ).fetchone()
    assert row[0] == "other"
    assert row[1] == idea  # verbatim
    # A builtin profile must not carry a user idea.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, user_idea,"
            " created_at, updated_at) VALUES (?, 'bad', 'Bad', 'builtin',"
            " 'should not be here', ?, ?)",
            (uuid.uuid4().hex, NOW_MS, NOW_MS),
        )


def test_transaction_helper_commits_and_rolls_back(conn):
    with transaction(conn):
        conn.execute(
            "INSERT INTO settings (key, value_json, updated_at)"
            " VALUES ('app.test', '1', ?)",
            (NOW_MS,),
        )
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1

    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute(
                "INSERT INTO settings (key, value_json, updated_at)"
                " VALUES ('app.bad', '1', ?)",
                (NOW_MS,),
            )
            raise RuntimeError("boom")
    assert conn.execute(
        "SELECT COUNT(*) FROM settings WHERE key = 'app.bad'"
    ).fetchone()[0] == 0
