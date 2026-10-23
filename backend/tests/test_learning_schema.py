"""Schema tests for the Learning Store (migration 0005 + runtime DDL).

Covers the DB layer only (store behaviour is a sibling suite):

* migration ``0005_learning_rules`` metadata (revision /
  down_revision) and DDL kept in sync with :mod:`app.learning.schema`;
* the migration's own upgrade() creates ``learning_rules`` with
  the contract columns, CHECKs, FK and indexes; its downgrade()
  removes the table + indexes and leaves the base schema intact
  (driven directly through an Alembic Operations context — while
  the Round 5 sibling migrations are still landing, the versions
  tree is temporarily multi-headed, so ``upgrade head`` cannot
  resolve yet; the chain seam is the coordinators' to close);
* :func:`app.learning.schema.ensure_learning_schema` is idempotent
  and produces a structure identical to the migrated one;
* FOREIGN KEY to ``channels`` is enforced with ON DELETE RESTRICT;
* CHECK constraints reject bad kind / status / version /
  evidence_count / confidence / thin_evidence values, both scope
  columns are NOT NULL, and the contract defaults are applied.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.learning import schema as learning_schema

BACKEND_DIR = Path(__file__).resolve().parent.parent

NOW_MS = 1_700_000_000_000

EXPECTED_COLUMNS = [
    "id",
    "channel_id",
    "niche",
    "kind",
    "statement_family",
    "version",
    "statement",
    "effect_value",
    "effect_unit",
    "evidence_count",
    "confidence",
    "thin_evidence",
    "status",
    "supersedes_id",
    "created_from_json",
    "created_at",
    "updated_at",
]

EXPECTED_INDEXES = {
    "idx_learning_rules_scope_status",
    "idx_learning_rules_family",
}


def _load_migration_0005():
    path = BACKEND_DIR / "migrations" / "versions" / "0005_learning_rules.py"
    spec = importlib.util.spec_from_file_location("migration_0005", path)
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


def test_migration_0005_metadata() -> None:
    module = _load_migration_0005()
    assert module.revision == "0005"
    assert module.down_revision == "0004"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_ddl_in_sync_with_runtime_schema() -> None:
    module = _load_migration_0005()
    assert module.LEARNING_RULES_DDL == learning_schema.LEARNING_RULES_DDL
    migration_source = (
        BACKEND_DIR / "migrations" / "versions" / "0005_learning_rules.py"
    ).read_text(encoding="utf-8")
    normalised_source = " ".join(migration_source.replace('"', " ").split())
    for statement in learning_schema.INDEX_STATEMENTS:
        assert " ".join(statement.split()) in normalised_source


# ---------------------------------------------------------------------------
# Migration upgrade / downgrade, driven directly through an Alembic
# Operations context.  (While the Round 5 sibling migrations are still
# landing, the versions tree is temporarily multi-headed, so
# ``command.upgrade(cfg, "head")`` cannot resolve; driving this
# migration's own upgrade()/downgrade() exercises exactly its DDL.)
# ---------------------------------------------------------------------------


def _run_migration(module, db_file: Path, direction: str) -> None:
    """Run the migration module's upgrade()/downgrade() against a real
    SQLite file through a SQLAlchemy-bound Alembic Operations context."""
    from sqlalchemy import create_engine

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            module.op = Operations(ctx)
            with ctx.begin_transaction():
                if direction == "up":
                    module.upgrade()
                else:
                    module.downgrade()
    finally:
        engine.dispose()


def _migrated_conn(db_file: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    _run_migration(_load_migration_0005(), db_file, "up")
    return sqlite3.connect(str(db_file))


def test_migration_0005_upgrade_and_downgrade(tmp_path):
    module = _load_migration_0005()
    db_file = tmp_path / "mig.db"
    conn = sqlite3.connect(str(db_file))
    try:
        # Minimal parent table so FK checks can run (the full base
        # schema is migration 0001's job, not this migration's).
        conn.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    _run_migration(module, db_file, "up")
    conn = sqlite3.connect(str(db_file))
    try:
        assert "learning_rules" in _tables(conn)
        cols = [
            row[1] for row in conn.execute("PRAGMA table_info(learning_rules)")
        ]
        assert cols == EXPECTED_COLUMNS
        assert EXPECTED_INDEXES <= _indexes(conn)

        sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'learning_rules'"
            ).fetchone()[0].split()
        )
        assert (
            "CHECK (kind IN ('topic_affinity', 'hook_pattern', "
            "'duration_band', 'title_pattern', 'thumbnail_style', "
            "'publish_window'))" in sql
        )
        assert (
            "CHECK (status IN ('candidate', 'active', 'superseded'))" in sql
        )
        assert "CHECK (evidence_count >= 0)" in sql
        assert "CHECK (thin_evidence IN (0, 1))" in sql

        # Scope columns are mandatory (NOT NULL) on every row.
        notnull = {
            row[1]: row[3]
            for row in conn.execute("PRAGMA table_info(learning_rules)")
        }
        assert notnull["channel_id"] == 1
        assert notnull["niche"] == 1

        # FK target + ON DELETE RESTRICT.
        assert _structure(conn, "learning_rules")["foreign_keys"] == [
            ("channels", "channel_id", "id", "RESTRICT")
        ]
    finally:
        conn.close()

    _run_migration(module, db_file, "down")
    conn = sqlite3.connect(str(db_file))
    try:
        tables = _tables(conn)
        assert "learning_rules" not in tables
        assert not (EXPECTED_INDEXES & _indexes(conn))
        assert "channels" in tables  # base schema untouched
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ensure_learning_schema: idempotent + identical to the migration
# ---------------------------------------------------------------------------


def test_ensure_learning_schema_idempotent_and_matches_migration(tmp_path):
    migrated = _migrated_conn(tmp_path / "migrated2.db")
    try:
        ensured = sqlite3.connect(":memory:")
        try:
            learning_schema.ensure_learning_schema(ensured)
            learning_schema.ensure_learning_schema(ensured)
            learning_schema.ensure_learning_schema(ensured)
            assert _structure(ensured, "learning_rules") == _structure(
                migrated, "learning_rules"
            )
            assert "learning_rules" in _tables(ensured)
            assert EXPECTED_INDEXES <= _indexes(ensured)
        finally:
            ensured.close()
    finally:
        migrated.close()


# ---------------------------------------------------------------------------
# FK enforcement + CHECK constraints + defaults (runtime schema)
# ---------------------------------------------------------------------------


def _conn_with_channel() -> sqlite3.Connection:
    """In-memory DB: minimal ``channels`` parent + Learning schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
    learning_schema.ensure_learning_schema(conn)
    conn.execute("INSERT INTO channels (id) VALUES ('chan-1')")
    conn.commit()
    return conn


def _insert_rule(conn: sqlite3.Connection, **overrides) -> str:
    row = {
        "id": uuid.uuid4().hex,
        "channel_id": "chan-1",
        "niche": "facts",
        "kind": "hook_pattern",
        "statement_family": "question-hook",
        "statement": "Question hooks retain better in the first 30s",
        "created_at": NOW_MS,
        "updated_at": NOW_MS,
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO learning_rules ({cols}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    return row["id"]


def test_fk_to_channels_enforced() -> None:
    conn = _conn_with_channel()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_rule(conn, channel_id="bogus-channel")
        conn.rollback()
    finally:
        conn.close()


def test_fk_on_delete_restrict() -> None:
    conn = _conn_with_channel()
    try:
        _insert_rule(conn)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM channels WHERE id = 'chan-1'")
        conn.rollback()
    finally:
        conn.close()


def test_scope_columns_not_null() -> None:
    conn = _conn_with_channel()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_rule(conn, channel_id=None)
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_rule(conn, niche=None)
        conn.rollback()
    finally:
        conn.close()


def test_defaults_applied_on_insert() -> None:
    conn = _conn_with_channel()
    try:
        rule_id = _insert_rule(conn)
        conn.commit()
        row = conn.execute(
            "SELECT version, evidence_count, thin_evidence, status,"
            " created_from_json, effect_value, confidence"
            " FROM learning_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        assert row == (1, 0, 1, "candidate", "[]", None, None)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "bogus"},
        {"status": "bogus"},
        {"version": 0},
        {"evidence_count": -1},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"thin_evidence": 2},
    ],
)
def test_learning_rules_check_constraints(overrides) -> None:
    conn = _conn_with_channel()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_rule(conn, **overrides)
        conn.rollback()
    finally:
        conn.close()
