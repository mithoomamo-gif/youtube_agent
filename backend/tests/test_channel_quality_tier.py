"""Per-channel quality tier tests (Round 5 — master encode plumbing).

Covered:

* vocabulary/default sync with the render path
  (``app.pipeline.render.renderer.QUALITY_TIERS``);
* storage: ``channels.quality_tier`` column in the fresh schema
  (``app.schema_ddl`` via ``init_db``), default ``draft``, lazy
  ensure on a legacy (pre-migration) database, migration 0008
  upgrade/downgrade driven directly through an Alembic Operations
  context (the sibling-test pattern), CHECK enforcement;
* persistence is per channel — two channels never leak tiers;
* API: ``quality_tier`` on channel list responses, ``PATCH
  /api/v1/channels/{id}`` updates it, invalid values are 422 and
  unknown channels 404;
* produce path (monolith): ``produce_video`` passes the spec's tier
  as ``quality_tier`` into the assemble/render entry point;
* scheduler path: a ``SchedulerTick`` materialises a project for a
  channel, and the spec seam the DAG stages use
  (``stage_bridge.project_to_spec`` + the handlers' channel
  context) resolves that channel's tier onto the spec, which
  ``stage_bridge`` then hands to the assemble stage.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import channel_quality
from app.channel_quality import (
    DEFAULT_QUALITY_TIER,
    QUALITY_TIERS,
    ensure_quality_tier_column,
    normalize_quality_tier,
    quality_tier_of_row,
    resolve_channel_quality_tier,
)
from tests.execution_fixtures import make_db, now_ms, seed_channel_niche

BACKEND_DIR = Path(__file__).resolve().parent.parent
NOW_MS = 1_700_000_000_000


# ---------------------------------------------------------------------------
# Vocabulary / normalisation / spec
# ---------------------------------------------------------------------------


def test_vocabulary_matches_render_path():
    from app.pipeline.render.renderer import (
        DEFAULT_QUALITY_TIER as RENDER_DEFAULT,
    )
    from app.pipeline.render.renderer import (
        QUALITY_TIERS as RENDER_TIERS,
    )

    assert tuple(RENDER_TIERS) == tuple(QUALITY_TIERS)
    assert RENDER_DEFAULT == DEFAULT_QUALITY_TIER == "draft"


def test_normalize_quality_tier():
    assert normalize_quality_tier(None) == "draft"
    assert normalize_quality_tier("") == "draft"
    assert normalize_quality_tier(" Master_1080P ") == "master_1080p"
    assert normalize_quality_tier("master_1440p_upscale") == (
        "master_1440p_upscale"
    )
    with pytest.raises(ValueError):
        normalize_quality_tier("master_4k")
    with pytest.raises(ValueError):
        normalize_quality_tier("1080p")


def test_project_spec_quality_tier_default_and_validation():
    from pydantic import ValidationError

    from app.integration.models import ProjectSpec

    assert ProjectSpec(topic="x").quality_tier == "draft"
    assert (
        ProjectSpec(topic="x", quality_tier="master_1080p").quality_tier
        == "master_1080p"
    )
    with pytest.raises(ValidationError):
        ProjectSpec(topic="x", quality_tier="ultra_hd")


# ---------------------------------------------------------------------------
# Storage: fresh schema, lazy ensure, migration 0008
# ---------------------------------------------------------------------------


def _cols(conn, table="channels"):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_fresh_db_has_column_and_channels_default_to_draft(tmp_path):
    _db_path, factory = make_db(tmp_path)
    channel_id, _niche_id = seed_channel_niche(factory)
    conn = factory()
    try:
        assert "quality_tier" in _cols(conn)
        row = conn.execute(
            "SELECT quality_tier FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()
        assert row[0] == "draft"  # column DEFAULT applied on insert
        assert resolve_channel_quality_tier(conn, channel_id) == "draft"
        assert quality_tier_of_row({"id": channel_id}) == "draft"
    finally:
        conn.close()


def test_check_constraint_rejects_invalid_stored_tier(tmp_path):
    _db_path, factory = make_db(tmp_path)
    channel_id, _ = seed_channel_niche(factory)
    conn = factory()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE channels SET quality_tier = 'bogus' WHERE id = ?",
                (channel_id,),
            )
    finally:
        conn.close()


def _legacy_conn() -> sqlite3.Connection:
    """A pre-migration channels table: every column except quality_tier."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE channels ("
        " id TEXT PRIMARY KEY, youtube_channel_id TEXT NOT NULL UNIQUE,"
        " title TEXT NOT NULL, language TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'active',"
        " created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,"
        " deleted_at INTEGER NULL)"
    )
    conn.execute(
        "INSERT INTO channels (id, youtube_channel_id, title, language,"
        " created_at, updated_at) VALUES ('legacy-1', 'UClegacy', 'Old',"
        " 'en', ?, ?)",
        (NOW_MS, NOW_MS),
    )
    conn.commit()
    return conn


def test_ensure_column_on_legacy_db_defaults_existing_rows_to_draft():
    conn = _legacy_conn()
    try:
        assert "quality_tier" not in _cols(conn)
        assert resolve_channel_quality_tier(conn, "legacy-1") == "draft"
        # The resolver ensured the column lazily; the row got the default.
        assert "quality_tier" in _cols(conn)
        assert conn.execute(
            "SELECT quality_tier FROM channels WHERE id = 'legacy-1'"
        ).fetchone()[0] == "draft"
        # Idempotent, and a flipped value survives a re-ensure.
        conn.execute(
            "UPDATE channels SET quality_tier = 'master_1080p'"
            " WHERE id = 'legacy-1'"
        )
        assert ensure_quality_tier_column(conn) is True
        assert resolve_channel_quality_tier(conn, "legacy-1") == (
            "master_1080p"
        )
        assert resolve_channel_quality_tier(conn, "no-such") == "draft"
    finally:
        conn.close()


def _load_migration_0008():
    path = BACKEND_DIR / "migrations" / "versions" / "0008_channel_quality_tier.py"
    spec = importlib.util.spec_from_file_location("migration_0008", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(module, db_file: Path, direction: str) -> None:
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


def test_migration_0008_metadata_upgrade_and_downgrade(tmp_path):
    module = _load_migration_0008()
    # 0005/0006/0007 were claimed by concurrent Round 5 siblings in
    # this tree (learning rules / comments / calibration log).
    assert module.revision == "0008"
    assert module.down_revision == "0007"

    db_file = tmp_path / "mig.db"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            "CREATE TABLE channels (id TEXT PRIMARY KEY,"
            " youtube_channel_id TEXT, title TEXT, deleted_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO channels VALUES ('c1', 'UC1', 'One', NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    _run_migration(module, db_file, "up")
    _run_migration(module, db_file, "up")  # guarded: second run is a no-op
    conn = sqlite3.connect(str(db_file))
    try:
        assert "quality_tier" in _cols(conn)
        assert conn.execute(
            "SELECT quality_tier FROM channels WHERE id = 'c1'"
        ).fetchone()[0] == "draft"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE channels SET quality_tier = 'bogus' WHERE id = 'c1'"
            )
        conn.rollback()
    finally:
        conn.close()

    _run_migration(module, db_file, "down")
    conn = sqlite3.connect(str(db_file))
    try:
        assert "quality_tier" not in _cols(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-channel persistence / isolation
# ---------------------------------------------------------------------------


def _set_tier(factory, channel_id, tier):
    conn = factory()
    try:
        conn.execute(
            "UPDATE channels SET quality_tier = ? WHERE id = ?",
            (tier, channel_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_tier_persists_per_channel_without_leak(tmp_path):
    _db_path, factory = make_db(tmp_path)
    chan_a, _ = seed_channel_niche(factory)
    chan_b, _ = seed_channel_niche(factory)
    _set_tier(factory, chan_a, "master_1440p_upscale")

    conn = factory()
    try:
        assert resolve_channel_quality_tier(conn, chan_a) == (
            "master_1440p_upscale"
        )
        assert resolve_channel_quality_tier(conn, chan_b) == "draft"
    finally:
        conn.close()

    _set_tier(factory, chan_b, "master_1080p")
    conn = factory()
    try:
        # Flipping B did not touch A (fresh connections: it persisted).
        assert resolve_channel_quality_tier(conn, chan_a) == (
            "master_1440p_upscale"
        )
        assert resolve_channel_quality_tier(conn, chan_b) == "master_1080p"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API: list exposes the field; PATCH writes it; invalid rejected
# ---------------------------------------------------------------------------


def _seed_api_channel(row_id: str, youtube_channel_id: str) -> None:
    from app import db as db_module

    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language,"
            " status, created_at, updated_at) VALUES (?, ?, ?, 'en',"
            " 'active', ?, ?)",
            (row_id, youtube_channel_id, f"Channel {row_id}", now, now),
        )
    finally:
        conn.close()


def _api_channel(client, row_id: str) -> dict | None:
    resp = client.get("/api/v1/channels")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        if item.get("id") == row_id:
            return item
    return None


def test_api_default_patch_and_isolation(app, client):
    suffix = uuid.uuid4().hex[:8]
    chan_a, chan_b = f"qt-a-{suffix}", f"qt-b-{suffix}"
    _seed_api_channel(chan_a, f"UCqtA{suffix}")
    _seed_api_channel(chan_b, f"UCqtB{suffix}")

    # Default is draft for both.
    assert _api_channel(client, chan_a)["quality_tier"] == "draft"
    assert _api_channel(client, chan_b)["quality_tier"] == "draft"

    resp = client.patch(
        f"/api/v1/channels/{chan_a}",
        json={"quality_tier": "master_1440p_upscale"},
    )
    assert resp.status_code == 200
    assert resp.json()["quality_tier"] == "master_1440p_upscale"

    # Persisted, and the other channel did not move.
    assert _api_channel(client, chan_a)["quality_tier"] == (
        "master_1440p_upscale"
    )
    assert _api_channel(client, chan_b)["quality_tier"] == "draft"


def test_api_invalid_tier_rejected_and_not_stored(app, client):
    suffix = uuid.uuid4().hex[:8]
    chan = f"qt-bad-{suffix}"
    _seed_api_channel(chan, f"UCqtBad{suffix}")

    resp = client.patch(
        f"/api/v1/channels/{chan}", json={"quality_tier": "ultra_hd"}
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == "quality_tier"
    assert _api_channel(client, chan)["quality_tier"] == "draft"

    empty = client.patch(f"/api/v1/channels/{chan}", json={})
    assert empty.status_code == 422

    missing = client.patch(
        "/api/v1/channels/no-such-channel",
        json={"quality_tier": "master_1080p"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "channel_not_found"


# ---------------------------------------------------------------------------
# Produce path (monolith): spec tier -> assemble entry point
# ---------------------------------------------------------------------------


def _fake_bundle():
    return SimpleNamespace(
        title="Test Title: Ocean Secrets",
        script_text="Chapter text.",
        narration_text="Chapter text.",
        thumbnail_text="Ocean Secrets",
        description="A test description.",
        tags=["test"],
        claims=[],
    )


def _patch_produce_stages(monkeypatch, tmp_path, received: dict):
    from app.integration import produce

    final = tmp_path / "final.mp4"
    final.write_bytes(b"fake-mp4")
    srt = tmp_path / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

    def fake_assemble(*args, **kwargs):
        received["quality_tier"] = kwargs.get("quality_tier")
        received["args"] = args
        return SimpleNamespace(
            final_mp4=str(final),
            duration_s=55.0,
            captions_srt=str(srt),
            subtitle_path_used="whisper-stub",
            thumbnails=[],
            qc_passed=True,
            qc_summary="all good",
            loudness_lufs=-14.2,
            render_path_used="ffmpeg-local",
            quality_tier=kwargs.get("quality_tier") or "draft",
            ledger_rows=[],
        )

    monkeypatch.setattr(produce, "build_content", lambda *a: _fake_bundle())
    monkeypatch.setattr(produce, "run_gate_a", lambda *a: (True, []))
    monkeypatch.setattr(
        produce,
        "synthesize_voice",
        lambda *a: SimpleNamespace(path_used="stub", ledger_rows=[]),
    )
    monkeypatch.setattr(
        produce,
        "plan_and_generate_visuals",
        lambda *a: SimpleNamespace(scene_manifest=[], ledger_rows=[]),
    )
    monkeypatch.setattr(produce, "assemble_project", fake_assemble)
    monkeypatch.setattr(
        produce,
        "finalize_publish",
        lambda *a: SimpleNamespace(
            gate_b_passed=True,
            manifest={"video_id": "fake-1", "state": "ready"},
            manifest_path=None,
            publish_state="ready_for_review",
            tokens=[],
        ),
    )


def test_produce_path_passes_channel_tier_to_assemble(monkeypatch, tmp_path):
    from app.integration import produce
    from app.integration.models import ProjectSpec

    received: dict = {}
    _patch_produce_stages(monkeypatch, tmp_path, received)
    spec = ProjectSpec(
        niche="facts",
        topic="Ocean depth secrets",
        minutes=1.0,
        channel_id="chan-master",
        quality_tier="master_1080p",
    )
    result = produce.produce_video(spec, tmp_path / "out")
    # The exact seam: the assemble/render entry point got quality_tier.
    assert received["quality_tier"] == "master_1080p"
    assert result.paths_used["quality_tier"] == "master_1080p"


def test_produce_path_defaults_to_draft(monkeypatch, tmp_path):
    from app.integration import produce
    from app.integration.models import ProjectSpec

    received: dict = {}
    _patch_produce_stages(monkeypatch, tmp_path, received)
    spec = ProjectSpec(niche="facts", topic="Ocean depth secrets", minutes=1.0)
    result = produce.produce_video(spec, tmp_path / "out")
    assert received["quality_tier"] == "draft"
    assert result.paths_used["quality_tier"] == "draft"


# ---------------------------------------------------------------------------
# Scheduler path: tick materialises the project; the spec seam and
# the handlers' channel context resolve the channel's tier
# ---------------------------------------------------------------------------


def _seed_schedule(factory, *, channel_id, niche_id, next_run_at):
    schedule_id = str(uuid.uuid4())
    ts = now_ms()
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO schedules (id, name, channel_id, niche_profile_id,"
            " cadence_json, topic_mode, target_minutes, publish_mode,"
            " is_enabled, next_run_at, created_at, updated_at) VALUES"
            " (?, 'QT schedule', ?, ?, ?, 'auto', 1, 'review_first', 1,"
            " ?, ?, ?)",
            (
                schedule_id,
                channel_id,
                niche_id,
                json.dumps({"type": "interval", "interval_hours": 24}),
                next_run_at,
                ts,
                ts,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return schedule_id


def test_scheduler_path_resolves_channel_tier_onto_spec(tmp_path):
    from app.execution.channel_context import resolve_channel_context
    from app.execution.scheduler import SchedulerTick
    from app.integration import stage_bridge

    db_path, factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(factory)
    _set_tier(factory, channel_id, "master_1080p")
    other_channel, _ = seed_channel_niche(factory)
    _seed_schedule(
        factory,
        channel_id=channel_id,
        niche_id=niche_id,
        next_run_at=now_ms() - 60_000,
    )

    summary = SchedulerTick(factory).tick(now_ms())
    assert summary["materialised"] == 1

    conn = factory()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        assert row is not None
        project = dict(row)
        assert conn.execute(
            "SELECT COUNT(*) FROM projects WHERE channel_id = ?",
            (other_channel,),
        ).fetchone()[0] == 0
    finally:
        conn.close()

    # The handlers' channel context carries this channel's tier.
    ctx = resolve_channel_context(factory, project["id"])
    assert ctx is not None
    assert ctx.quality_tier == "master_1080p"
    assert ctx.to_dict()["quality_tier"] == "master_1080p"

    # The spec seam both paths share resolves the same tier.
    spec = stage_bridge.project_to_spec(
        project, {"db_path": str(db_path)}
    )
    assert spec.channel_id == channel_id
    assert spec.quality_tier == "master_1080p"

    # ...and the bridge hands exactly that tier to the assemble stage.
    received: dict = {}

    def fake_assemble(*args, **kwargs):
        received["quality_tier"] = kwargs.get("quality_tier")
        return SimpleNamespace(final_mp4="x")

    stage_bridge._call_assemble_project(
        fake_assemble,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(spec=spec),
    )
    assert received["quality_tier"] == "master_1080p"


def test_scheduler_path_channel_context_beats_stale_row_value(tmp_path):
    """The handlers inject ``channel_context`` into project+payload;
    project_to_spec honours it over the raw row (same resolution)."""
    from app.integration import stage_bridge

    db_path, factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(factory)
    _set_tier(factory, channel_id, "master_1080p")
    project = {
        "id": str(uuid.uuid4()),
        "channel_id": channel_id,
        "niche_profile_id": niche_id,
        "topic": "Ocean depth secrets",
        "publish_mode": "review_first",
    }
    spec = stage_bridge.project_to_spec(
        project,
        {
            "db_path": str(db_path),
            "channel_context": {"quality_tier": "master_1440p_upscale"},
        },
    )
    assert spec.quality_tier == "master_1440p_upscale"

    # An invalid tier anywhere in the chain fails loud, not draft.
    with pytest.raises(ValueError):
        stage_bridge.project_to_spec(
            project,
            {"db_path": str(db_path), "quality_tier": "bogus"},
        )
