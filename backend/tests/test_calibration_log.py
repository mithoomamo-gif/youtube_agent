"""Calibration log tests (Round 5 — Learning Loop): schema, migration
0009, recording seam (idempotency, validation, evidence adapter),
scope isolation and the report read model.

Migration number: 0009 — 0005 (learning_rules), 0006 (comments) and
0007 (analytics) were claimed by sibling Round 5 teams and 0008 by
the channel quality tier team; calibration was renumbered from
0008 to 0009 at Round 5 closure (R5-001).  See
app.calibration.schema.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.calibration import schema as calib_schema
from app.calibration.report import build_calibration_report
from app.calibration.store import (
    CalibrationStore,
    record_calibration_entry,
    record_video_evidence,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent

NOW_MS = 1_700_000_000_000

EXPECTED_COLUMNS = [
    "id",
    "project_id",
    "youtube_video_id",
    "channel_id",
    "niche",
    "metric",
    "snapshot_point",
    "predicted_low",
    "predicted_mid",
    "predicted_high",
    "actual_value",
    "snapshot_ref",
    "prediction_issued_at",
    "published_at",
    "created_at",
]

EXPECTED_INDEXES = {
    "idx_calibration_log_scope",
    "idx_calibration_log_project",
    "idx_calibration_log_video",
    "uq_calibration_log_entry",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    """In-memory DB with minimal parent tables + the calibration log."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
    calib_schema.ensure_calibration_schema(conn)
    conn.execute("INSERT INTO channels (id) VALUES ('channel-1')")
    conn.execute("INSERT INTO channels (id) VALUES ('channel-2')")
    conn.commit()
    return conn


def _store() -> tuple[CalibrationStore, sqlite3.Connection]:
    conn = _conn()
    return CalibrationStore(conn), conn


def _record(store: CalibrationStore, video: str = "v1", **overrides):
    # The store FK-references projects(id): stub the project row the
    # same way _conn stubs channels.
    project_id = overrides.get("project_id", f"proj-{video}")
    if project_id is not None:
        store._shared_conn.execute(
            "INSERT OR IGNORE INTO projects (id) VALUES (?)", (project_id,)
        )
        store._shared_conn.commit()
    kwargs = {
        "channel_id": "channel-1",
        "niche": "facts",
        "snapshot_point": "D+7",
        "metric": "views",
        "project_id": f"proj-{video}",
        "youtube_video_id": video,
        "predicted_low": 800,
        "predicted_mid": 1000,
        "predicted_high": 1200,
        "actual_value": 1100,
        "snapshot_ref": f"snap-{video}-d7",
    }
    kwargs.update(overrides)
    return store.record_entry(**kwargs)


def _count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM calibration_log").fetchone()[0]


# ---------------------------------------------------------------------------
# Migration metadata + DDL sync
# ---------------------------------------------------------------------------


def _load_migration_0009():
    path = BACKEND_DIR / "migrations" / "versions" / "0009_calibration_log.py"
    spec = importlib.util.spec_from_file_location("migration_0009", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0009_metadata() -> None:
    module = _load_migration_0009()
    assert module.revision == "0009"
    assert module.down_revision == "0008"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_ddl_in_sync_with_runtime_schema() -> None:
    module = _load_migration_0009()
    assert module.CALIBRATION_LOG_DDL == calib_schema.CALIBRATION_LOG_DDL
    migration_source = (
        BACKEND_DIR / "migrations" / "versions" / "0009_calibration_log.py"
    ).read_text(encoding="utf-8")
    normalised_source = " ".join(migration_source.replace('"', " ").split())
    for statement in calib_schema.INDEX_STATEMENTS:
        assert " ".join(statement.split()) in normalised_source


def test_migration_0009_upgrade_and_downgrade_via_operations(tmp_path):
    """Drive migration 0009's upgrade()/downgrade() through a real
    Alembic Operations context on a fresh DB.

    NOTE (documented, deliberate): this does NOT use
    ``alembic upgrade head`` over the shared versions directory.
    At the time of writing that directory contained duplicate
    revision ids from sibling Round 5 teams (0008 x2 —
    ``0008_channel_quality_tier`` landed minutes after this team's
    ``0008_calibration_log``), which made the global chain unloadable;
    the Round 5 closure pass (R5-001) resolved it by renumbering
    this migration to 0009, chained on 0008.  Exercising this
    migration's own upgrade/downgrade in isolation verifies exactly
    what this team owns; the runtime ``ensure_calibration_schema``
    path (the repo's primary mechanism, per the Style Lab / Learning
    Store pattern) is covered separately below.
    """
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from sqlalchemy import create_engine

    module = _load_migration_0009()
    db_file = tmp_path / "migration.db"
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.begin() as sa_conn:
        sa_conn.exec_driver_sql("CREATE TABLE channels (id TEXT PRIMARY KEY)")
        sa_conn.exec_driver_sql("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        ctx = MigrationContext.configure(sa_conn)
        with Operations.context(ctx):
            module.upgrade()

    conn = sqlite3.connect(str(db_file))
    try:
        cols = [
            row[1] for row in conn.execute("PRAGMA table_info(calibration_log)")
        ]
        assert cols == EXPECTED_COLUMNS
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert EXPECTED_INDEXES <= indexes
        sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'calibration_log'"
            ).fetchone()[0].split()
        )
        assert "CHECK (snapshot_point IN ('D+7', 'D+28'))" in sql
        fks = sorted(
            (row[2], row[3], row[6])
            for row in conn.execute("PRAGMA foreign_key_list(calibration_log)")
        )
        assert ("channels", "channel_id", "RESTRICT") in fks
        assert ("projects", "project_id", "RESTRICT") in fks

        with engine.begin() as sa_conn:
            ctx = MigrationContext.configure(sa_conn)
            with Operations.context(ctx):
                module.downgrade()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "calibration_log" not in tables
        assert "channels" in tables  # parent schema survives
    finally:
        conn.close()


def test_ensure_calibration_schema_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        calib_schema.ensure_calibration_schema(conn)
        calib_schema.ensure_calibration_schema(conn)
        calib_schema.ensure_calibration_schema(conn)
        cols = [
            row[1] for row in conn.execute("PRAGMA table_info(calibration_log)")
        ]
        assert cols == EXPECTED_COLUMNS
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema constraints
# ---------------------------------------------------------------------------


def _raw_insert(conn: sqlite3.Connection, **overrides) -> None:
    row = {
        "id": uuid.uuid4().hex,
        "project_id": None,
        "youtube_video_id": "v-raw",
        "channel_id": "channel-1",
        "niche": "facts",
        "metric": "views",
        "snapshot_point": "D+7",
        "predicted_low": 800.0,
        "predicted_mid": 1000.0,
        "predicted_high": 1200.0,
        "actual_value": 1100.0,
        "created_at": NOW_MS,
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO calibration_log ({cols}) VALUES ({placeholders})",
        tuple(row.values()),
    )


def test_fk_to_channels_enforced() -> None:
    conn = _conn()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _raw_insert(conn, channel_id="bogus-channel")
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"snapshot_point": "D+3"},
        {"predicted_low": 1300.0},                    # low > mid
        {"predicted_mid": 1300.0},                    # mid > high
        {"predicted_low": -1.0, "predicted_mid": 0.0},  # negative low
        {"project_id": None, "youtube_video_id": None},  # no video identity
    ],
)
def test_check_constraints(overrides) -> None:
    conn = _conn()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _raw_insert(conn, **overrides)
        conn.rollback()
    finally:
        conn.close()


def test_unique_index_backstops_duplicate_entry() -> None:
    conn = _conn()
    try:
        _raw_insert(conn)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _raw_insert(conn)
        conn.rollback()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recording seam: happy path + validation
# ---------------------------------------------------------------------------


def test_record_entry_stores_issued_band_and_actual() -> None:
    store, conn = _store()
    try:
        row, created = _record(store)
        assert created is True
        assert row["channel_id"] == "channel-1"
        assert row["niche"] == "facts"
        assert row["metric"] == "views"
        assert row["snapshot_point"] == "D+7"
        assert (row["predicted_low"], row["predicted_mid"], row["predicted_high"]) == (
            800,
            1000,
            1200,
        )
        assert row["actual_value"] == 1100
        assert row["snapshot_ref"] == "snap-v1-d7"
        assert row["project_id"] == "proj-v1"
        assert row["youtube_video_id"] == "v1"
    finally:
        conn.close()


def test_record_entry_validation() -> None:
    store, conn = _store()
    try:
        with pytest.raises(ValueError):
            _record(store, snapshot_point="D+3")
        with pytest.raises(ValueError):
            _record(store, predicted_low=1500)  # low > mid
        with pytest.raises(ValueError):
            _record(store, project_id=None, youtube_video_id=None)
        with pytest.raises(ValueError):
            _record(store, niche="")
        with pytest.raises(ValueError):
            _record(store, actual_value=float("nan"))
        with pytest.raises(ValueError):
            _record(store, predicted_low=None, predicted_mid=None,
                    predicted_high=None, expectation_band=None)
        assert _count(conn) == 0
    finally:
        conn.close()


def test_record_entry_from_band_object_without_mid_uses_band_centre() -> None:
    store, conn = _store()
    try:
        band = SimpleNamespace(low=800, high=1200, metric="views")
        row, created = store.record_entry(
            channel_id="channel-1",
            niche="facts",
            snapshot_point="D+7",
            youtube_video_id="v-band",
            expectation_band=band,
            actual_value=1000,
        )
        assert created is True
        assert row["predicted_mid"] == 1000  # (800 + 1200) / 2
        assert row["metric"] == "views"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recording seam: idempotency
# ---------------------------------------------------------------------------


def test_record_entry_idempotent_same_video_point_metric() -> None:
    store, conn = _store()
    try:
        first, created1 = _record(store)
        # Same video + point + metric, even with a different actual:
        # the first recorded actual stands, nothing is rewritten.
        second, created2 = _record(store, actual_value=9999)
        assert created1 is True
        assert created2 is False
        assert second["id"] == first["id"]
        assert second["actual_value"] == 1100
        assert _count(conn) == 1
    finally:
        conn.close()


def test_record_entry_idempotent_across_identifier_forms() -> None:
    store, conn = _store()
    try:
        _record(store)  # recorded with project_id AND youtube_video_id
        by_video, created = _record(store, project_id=None)
        assert created is False
        by_project, created2 = _record(store, youtube_video_id=None)
        assert created2 is False
        assert by_video["id"] == by_project["id"]
        assert _count(conn) == 1
    finally:
        conn.close()


def test_record_entry_distinct_point_or_metric_is_a_new_row() -> None:
    store, conn = _store()
    try:
        _record(store)
        _, created_d28 = _record(store, snapshot_point="D+28")
        _, created_metric = _record(store, metric="retention_pct")
        _, created_video = _record(store, video="v2")
        assert created_d28 is True
        assert created_metric is True
        assert created_video is True
        assert _count(conn) == 4
    finally:
        conn.close()


def test_module_level_seam_and_conn_factory() -> None:
    conn = _conn()
    try:
        row, created = record_calibration_entry(
            conn,
            channel_id="channel-1",
            niche="facts",
            snapshot_point="D+28",
            youtube_video_id="v9",
            predicted_low=10,
            predicted_mid=20,
            predicted_high=30,
            actual_value=25,
        )
        assert created is True
        assert row["snapshot_point"] == "D+28"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Evidence adapter (update-engine seam)
# ---------------------------------------------------------------------------


def test_record_video_evidence_adapter() -> None:
    store, conn = _store()
    try:
        evidence = SimpleNamespace(
            video_id="vid-ev",
            channel_id="channel-1",
            niche="facts",
            snapshot_point="D+7",
            views=1500,
            outcome=1500.0,
            expectation_band=SimpleNamespace(low=1000, high=2000, metric="views"),
            published_at_ms=NOW_MS,
            snapshot_at_ms=NOW_MS + 7 * 24 * 3600 * 1000,
        )
        row, created = record_video_evidence(store, evidence, snapshot_ref="snap-ev")
        assert created is True
        assert row["youtube_video_id"] == "vid-ev"
        assert row["actual_value"] == 1500
        assert row["predicted_mid"] == 1500  # band centre
        assert row["published_at"] == NOW_MS
        # Re-delivery of the same snapshot is idempotent.
        _, created2 = store.record_video_evidence(evidence)
        assert created2 is False
        assert _count(conn) == 1
    finally:
        conn.close()


def test_record_video_evidence_without_band_refused() -> None:
    store, conn = _store()
    try:
        evidence = SimpleNamespace(
            video_id="vid-x",
            channel_id="channel-1",
            niche="facts",
            snapshot_point="D+7",
            views=10,
            outcome=10.0,
            expectation_band=None,
        )
        with pytest.raises(ValueError):
            store.record_video_evidence(evidence)
        assert _count(conn) == 0
    finally:
        conn.close()


def test_record_video_evidence_real_contracts() -> None:
    contracts = pytest.importorskip("app.learning_loop.contracts")
    store, conn = _store()
    try:
        evidence = contracts.VideoEvidence(
            video_id="vid-real",
            channel_id="channel-1",
            niche="facts",
            snapshot_point="D+28",
            views=900,
            expectation_band=contracts.ExpectationBand(
                low=800, high=1200, metric="views"
            ),
        )
        row, created = store.record_video_evidence(evidence)
        assert created is True
        assert row["actual_value"] == 900
        assert row["predicted_mid"] == 1000
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scope isolation + report read model
# ---------------------------------------------------------------------------

# Same hand-computed fixture as test_calibration_math (bias +2.5,
# MAPE 27.5, coverage 0.5 at D+7 for channel-1 / facts / views).
_FACTS_D7 = [
    ("v1", 800, 1000, 1200, 1100),
    ("v2", 1500, 2000, 2500, 1000),
    ("v3", 400, 500, 600, 600),
    ("v4", 900, 1000, 1100, 1300),
]


def _seed_facts(store: CalibrationStore) -> None:
    for video, low, mid, high, actual in _FACTS_D7:
        _record(
            store,
            video=video,
            predicted_low=low,
            predicted_mid=mid,
            predicted_high=high,
            actual_value=actual,
        )


def test_report_scope_isolation_between_niches_and_channels() -> None:
    store, conn = _store()
    try:
        _seed_facts(store)
        before = build_calibration_report(store, channel_id="channel-1", niche="facts")
        facts_scope = before["scopes"][0]
        assert facts_scope["points"]["D+7"]["signed_bias_pct"] == pytest.approx(2.5)
        assert facts_scope["points"]["D+7"]["mape_pct"] == pytest.approx(27.5)
        assert facts_scope["points"]["D+7"]["band_coverage"] == pytest.approx(0.5)

        # Poison the neighbouring scopes: wild niche-B misses on the
        # same channel, and same-niche rows on another channel.
        for i in range(4):
            _record(
                store,
                video=f"horror-{i}",
                niche="horror",
                predicted_low=9000,
                predicted_mid=10000,
                predicted_high=11000,
                actual_value=10,
            )
            _record(
                store,
                video=f"other-{i}",
                channel_id="channel-2",
                predicted_low=9000,
                predicted_mid=10000,
                predicted_high=11000,
                actual_value=10,
            )

        after = build_calibration_report(store, channel_id="channel-1", niche="facts")
        assert len(after["scopes"]) == 1
        facts_after = after["scopes"][0]
        # Niche B / channel-2 rows changed NOTHING about niche A's math.
        assert facts_after["points"] == facts_scope["points"]
        assert facts_after["entry_count"] == 4
        assert {r["niche"] for r in after["rows"]} == {"facts"}
        assert {r["channel_id"] for r in after["rows"]} == {"channel-1"}

        # The unfiltered report keeps all three scopes separate.
        full = build_calibration_report(store)
        keys = {(s["channel_id"], s["niche"]) for s in full["scopes"]}
        assert keys == {
            ("channel-1", "facts"),
            ("channel-1", "horror"),
            ("channel-2", "facts"),
        }
        horror = next(s for s in full["scopes"] if s["niche"] == "horror")
        # (10 - 10000) / 10000 = -99.9% on every row.
        assert horror["points"]["D+7"]["signed_bias_pct"] == pytest.approx(-99.9)
        assert horror["points"]["D+7"]["band_coverage"] == pytest.approx(0.0)
    finally:
        conn.close()


def test_report_includes_misses_and_labels_small_n() -> None:
    store, conn = _store()
    try:
        _seed_facts(store)
        # A second niche with only 2 videos: rows shown, stats labelled.
        _record(store, video="h1", niche="horror", actual_value=1100)
        _record(store, video="h2", niche="horror", actual_value=5)
        report = build_calibration_report(store)
        horror = next(s for s in report["scopes"] if s["niche"] == "horror")
        assert horror["entry_count"] == 2
        assert len(horror["rows"]) == 2
        assert horror["points"]["D+7"]["evidence"] == "insufficient_evidence"
        assert horror["points"]["D+7"]["label"] == "insufficient evidence"
        assert horror["points"]["D+7"]["mape_pct"] is None

        facts = next(s for s in report["scopes"] if s["niche"] == "facts")
        verdicts = {r["youtube_video_id"]: r["verdict"] for r in facts["rows"]}
        # The misses are in the report, named as misses — not dropped.
        assert verdicts == {
            "v1": "within_band",
            "v2": "below_band",
            "v3": "within_band",
            "v4": "above_band",
        }
        assert facts["video_count"] == 4
        assert len(report["rows"]) == 6
    finally:
        conn.close()


def test_list_entries_scope_filters_are_exact() -> None:
    store, conn = _store()
    try:
        _record(store, video="a", niche="facts")
        _record(store, video="b", niche="facts-2")  # near-name, other niche
        _record(store, video="c", niche="facts", channel_id="channel-2")
        rows = store.list_entries(channel_id="channel-1", niche="facts")
        assert [r["youtube_video_id"] for r in rows] == ["a"]
    finally:
        conn.close()
