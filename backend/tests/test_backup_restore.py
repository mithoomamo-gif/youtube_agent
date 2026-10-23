"""Round 6 — backup export / restore tests.

Covers the required evidence for the Backup/Restore workstream:

* round-trip: seed a DB with a channel + schedule + Style Lab style
  profile (+ competitor metadata), export a bundle, wipe the live DB,
  import, and assert the data is back and equivalent;
* downgrade protection: a bundle from a NEWER DB schema is refused
  (409 ``backup_from_newer_app``) and the live DB is untouched;
* corrupt bundles are refused (not a zip / missing manifest / damaged
  database payload) and the live DB is untouched;
* secret hygiene: no raw secret values in the bundle — secret-shaped
  fields are stripped from the JSON snapshots, files that live
  outside the DB (a secret-store sentinel, competitor video files)
  never enter the bundle at all;
* the FastAPI endpoints (info / export download / import upload).
"""

from __future__ import annotations

import functools
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app import backup as backup_module
from app.backup import BackupError, build_export, restore_backup
from app import db as db_module

NOW_MS = 1_700_000_000_000
CHANNEL_ID = "chan-backup-1"
NICHE_ID = "niche-backup-1"
SCHEDULE_ID = "sched-backup-1"


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _stamp_alembic_head(conn: sqlite3.Connection, revision: str = "0010") -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alembic_version "
        "(version_num VARCHAR(32) NOT NULL)"
    )
    conn.execute("DELETE FROM alembic_version")
    conn.execute(
        "INSERT INTO alembic_version (version_num) VALUES (?)", (revision,)
    )


def seed_database(db_path: Path) -> dict:
    """Create a DB with one channel, one schedule, one style profile."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db_module.init_db(db_path)
    try:
        _stamp_alembic_head(conn)
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, "
            "created_at, updated_at) VALUES (?, 'facts', 'Facts', "
            "'builtin', ?, ?)",
            (NICHE_ID, NOW_MS, NOW_MS),
        )
        config = {
            "connect_status": "connected",
            "credential_status": "active",
            # Secret-shaped plants: must be stripped from the JSON
            # snapshot (the DB itself never holds real secrets — they
            # live in the secret store — this is defence in depth).
            "api_key": "PLANTED-SNAPSHOT-SECRET",
            "refresh_token": "PLANTED-SNAPSHOT-TOKEN",
            "note": "keep-me",
        }
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, "
            "handle, language, country, status, config_json, "
            "created_at, updated_at) VALUES (?, 'UCbackup123', "
            "'Backup Test Channel', '@backuptest', 'en', 'PK', "
            "'active', ?, ?, ?)",
            (CHANNEL_ID, json.dumps(config), NOW_MS, NOW_MS),
        )
        conn.execute(
            "INSERT INTO schedules (id, name, channel_id, "
            "niche_profile_id, cadence_json, topic_mode, "
            "publish_mode, is_enabled, created_at, updated_at) "
            "VALUES (?, 'Daily facts', ?, ?, ?, 'auto', "
            "'review_first', 1, ?, ?)",
            (
                SCHEDULE_ID,
                CHANNEL_ID,
                NICHE_ID,
                json.dumps({"type": "daily", "time": "09:30"}),
                NOW_MS,
                NOW_MS,
            ),
        )
    finally:
        conn.close()

    # Style Lab rows go through the real store (it ensures its schema).
    from app.style_lab.store import StyleLabStore

    store = StyleLabStore(functools.partial(db_module.connect, db_path))
    competitor = store.create_competitor(
        CHANNEL_ID,
        {
            "source_kind": "handle",
            "handle": "rivalchannel",
            "channel_url": "https://www.youtube.com/@rivalchannel",
            "source_input": "@rivalchannel",
        },
        display_name="Rival Channel",
    )
    video = store.add_video(
        competitor["id"],
        {
            "source": "attached_file",
            "title": "Rival video",
            "storage_path": "style_lab/chan/comp/deadbeef.mp4",
            "file_size_bytes": 12345,
        },
    )
    profile = store.create_profile(
        CHANNEL_ID, "Rival style", [competitor["id"]]
    )
    profile_json = json.dumps(
        {"schema_version": "style_profile.v1", "hook_style": "question"}
    )
    conn = db_module.connect(db_path)
    try:
        conn.execute(
            "UPDATE style_profiles SET profile_json = ? WHERE id = ?",
            (profile_json, profile["id"]),
        )
    finally:
        conn.close()
    return {
        "competitor_id": competitor["id"],
        "video_id": video["id"],
        "profile_id": profile["id"],
        "profile_json": profile_json,
    }


def _read_row(db_path: Path, table: str, row_id: str) -> dict | None:
    conn = db_module.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _fresh_empty_db(db_path: Path) -> None:
    """A live-but-empty install (what restore replaces)."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    conn = db_module.init_db(db_path)
    try:
        _stamp_alembic_head(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Round-trip (required evidence)
# ---------------------------------------------------------------------------


def test_backup_round_trip_restores_channel_schedule_and_style_profile(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    db_path = data_dir / "app.db"
    seeded = seed_database(db_path)

    # Files that must NEVER enter the bundle: a secret-store sentinel
    # and a raw competitor video file living under the data root.
    secret_file = data_dir / "secret-store.json"
    secret_file.write_text('{"value": "SENTINEL-SECRET-STORE-VALUE"}')
    video_path = data_dir / "style_lab" / "chan" / "comp" / "deadbeef.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"SENTINEL-COMPETITOR-VIDEO-BYTES")

    bundle_path = tmp_path / "bundle.zip"
    result = build_export(db_path, bundle_path)
    manifest = result["manifest"]

    # -- manifest shape -------------------------------------------------
    assert manifest["bundle_format_version"] == 1
    assert manifest["db_schema_version"] == 10
    assert manifest["app_version"]
    assert manifest["created_at"]
    included_paths = {item["path"] for item in manifest["included"]}
    assert "database/app.db" in included_paths
    excluded_text = json.dumps(manifest["excluded"]).lower()
    assert "secret" in excluded_text
    assert "competitor video" in excluded_text
    assert manifest["counts"]["channels"] == 1
    assert manifest["counts"]["schedules"] == 1
    assert manifest["counts"]["style_profiles"] == 1

    # -- bundle contents --------------------------------------------------
    raw = bundle_path.read_bytes()
    assert b"SENTINEL-SECRET-STORE-VALUE" not in raw
    assert b"SENTINEL-COMPETITOR-VIDEO-BYTES" not in raw
    with zipfile.ZipFile(bundle_path) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "database/app.db" in names
        assert "data/channels.json" in names
        assert "data/schedules.json" in names
        assert "data/style_profiles.json" in names
        assert "data/competitors.json" in names
        assert not any(name.endswith(".mp4") for name in names)
        channels_snapshot = json.loads(archive.read("data/channels.json"))
        competitors_snapshot = json.loads(
            archive.read("data/competitors.json")
        )

    # Secret-shaped fields are stripped from the readable snapshots…
    snapshot_text = json.dumps(channels_snapshot)
    assert "PLANTED-SNAPSHOT-SECRET" not in snapshot_text
    assert "PLANTED-SNAPSHOT-TOKEN" not in snapshot_text
    # …while the actual config survives.
    assert channels_snapshot[0]["title"] == "Backup Test Channel"
    assert channels_snapshot[0]["config_json"]["note"] == "keep-me"
    # Competitor metadata (not the file) is in the bundle.
    videos = competitors_snapshot["competitor_videos"]
    assert videos and videos[0]["storage_path"].endswith("deadbeef.mp4")

    # -- wipe + restore ---------------------------------------------------
    before_channel = _read_row(db_path, "channels", CHANNEL_ID)
    before_schedule = _read_row(db_path, "schedules", SCHEDULE_ID)
    before_profile = _read_row(
        db_path, "style_profiles", seeded["profile_id"]
    )
    _fresh_empty_db(db_path)
    assert _read_row(db_path, "channels", CHANNEL_ID) is None

    summary = restore_backup(bundle_path, data_dir, db_path)

    assert summary["restored"] is True
    assert summary["db_schema_version"] == 10
    assert summary["counts"] == {
        "channels": 1,
        "schedules": 1,
        "style_profiles": 1,
    }
    # A safety copy of the pre-restore DB was kept.
    assert summary["safety_copy"] is not None
    assert (data_dir / "backups" / summary["safety_copy"]).is_file()

    # The data is back and equivalent.
    assert _read_row(db_path, "channels", CHANNEL_ID) == before_channel
    assert _read_row(db_path, "schedules", SCHEDULE_ID) == before_schedule
    after_profile = _read_row(db_path, "style_profiles", seeded["profile_id"])
    assert after_profile == before_profile
    assert after_profile["profile_json"] == seeded["profile_json"]
    video_row = _read_row(db_path, "competitor_videos", seeded["video_id"])
    assert video_row is not None
    assert video_row["title"] == "Rival video"


def test_restore_older_schema_backup_is_accepted(tmp_path: Path):
    """An older (compatible) schema backup restores cleanly."""
    data_dir = tmp_path / "data"
    db_path = data_dir / "app.db"
    seed_database(db_path)
    # Rewind the source DB to an older schema stamp, then export.
    conn = db_module.connect(db_path)
    try:
        _stamp_alembic_head(conn, "0007")
    finally:
        conn.close()
    bundle_path = tmp_path / "old.zip"
    build_export(db_path, bundle_path)

    _fresh_empty_db(db_path)
    summary = restore_backup(bundle_path, data_dir, db_path)
    assert summary["restored"] is True
    assert summary["db_schema_version"] == 7
    assert _read_row(db_path, "channels", CHANNEL_ID) is not None


# ---------------------------------------------------------------------------
# Downgrade protection + corruption
# ---------------------------------------------------------------------------


def test_restore_refuses_newer_schema_bundle_and_changes_nothing(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    db_path = data_dir / "app.db"
    seed_database(db_path)

    # A bundle honestly made by a newer app: schema 999 in the DB and
    # therefore in the manifest.
    future_dir = tmp_path / "future"
    future_db = future_dir / "app.db"
    seed_database(future_db)
    conn = db_module.connect(future_db)
    try:
        _stamp_alembic_head(conn, "0999")
    finally:
        conn.close()
    future_bundle = tmp_path / "future.zip"
    result = build_export(future_db, future_bundle)
    assert result["manifest"]["db_schema_version"] == 999

    live_hash_before = _read_row(db_path, "channels", CHANNEL_ID)
    with pytest.raises(BackupError) as excinfo:
        restore_backup(future_bundle, data_dir, db_path)
    err = excinfo.value
    assert err.code == "backup_from_newer_app"
    assert err.status_code == 409
    assert "newer version of the app" in err.message
    assert "Update the app to restore it" in err.message
    # Live data untouched — never partially applied.
    assert _read_row(db_path, "channels", CHANNEL_ID) == live_hash_before
    assert not list((data_dir / "backups").glob("pre-restore-*"))


def test_restore_refuses_corrupt_bundles(tmp_path: Path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "app.db"
    seed_database(db_path)
    channel_before = _read_row(db_path, "channels", CHANNEL_ID)

    not_a_zip = tmp_path / "junk.zip"
    not_a_zip.write_bytes(b"this is not a zip file at all" * 10)

    no_manifest = tmp_path / "no-manifest.zip"
    with zipfile.ZipFile(no_manifest, "w") as archive:
        archive.writestr("database/app.db", b"whatever")

    good_bundle = tmp_path / "good.zip"
    build_export(db_path, good_bundle)
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(good_bundle.read_bytes()[:64])

    # Tampered payload: DB bytes changed after the manifest (with its
    # checksum) was written.
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(good_bundle) as src:
        entries = {name: src.read(name) for name in src.namelist()}
    entries["database/app.db"] = entries["database/app.db"][:-16] + (
        b"\x00" * 16
    )
    with zipfile.ZipFile(tampered, "w") as dst:
        for name, payload in entries.items():
            dst.writestr(name, payload)

    for bad in (not_a_zip, no_manifest, truncated, tampered):
        with pytest.raises(BackupError) as excinfo:
            restore_backup(bad, data_dir, db_path)
        assert excinfo.value.code == "backup_corrupt", bad.name
        assert _read_row(db_path, "channels", CHANNEL_ID) == channel_before


def test_export_without_database_is_a_clear_error(tmp_path: Path):
    with pytest.raises(BackupError) as excinfo:
        build_export(tmp_path / "nope" / "app.db", tmp_path / "x.zip")
    assert excinfo.value.code == "backup_no_database"


def test_current_schema_version_matches_alembic_head():
    # The supported schema must track the real Alembic head (0010 at
    # Round 6 start; later migrations raise it) — never a stale literal.
    import re

    versions_dir = (
        Path(__file__).resolve().parent.parent / "migrations" / "versions"
    )
    heads = []
    for path in versions_dir.glob("*.py"):
        match = re.search(
            r'^revision(?::\s*str)?\s*=\s*["\'](\d+)["\']',
            path.read_text(encoding="utf-8"),
            re.M,
        )
        if match:
            heads.append(int(match.group(1)))
    assert heads and max(heads) >= 10
    assert backup_module.CURRENT_DB_SCHEMA_VERSION == max(heads)


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def _post_import(client, payload: bytes, filename: str = "backup.zip"):
    return client.post(
        "/api/v1/settings/backup/import",
        files={"file": (filename, io.BytesIO(payload), "application/zip")},
    )


def test_backup_api_info_and_export(client):
    info = client.get("/api/v1/settings/backup")
    assert info.status_code == 200
    body = info.json()
    assert body["bundle_format_version"] == 1
    assert (
        body["supported_db_schema_version"]
        == backup_module.CURRENT_DB_SCHEMA_VERSION
    )
    assert body["database_present"] is True

    resp = client.get("/api/v1/settings/backup/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert "database/app.db" in archive.namelist()
    assert manifest["bundle_format_version"] == 1
    assert (
        manifest["db_schema_version"]
        <= backup_module.CURRENT_DB_SCHEMA_VERSION
    )
    assert any(
        "secret" in item["what"].lower() for item in manifest["excluded"]
    )


def test_backup_api_import_round_trip_and_newer_refusal(client, settings):
    exported = client.get("/api/v1/settings/backup/export")
    assert exported.status_code == 200

    restored = _post_import(client, exported.content)
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["restored"] is True
    assert body["safety_copy"]
    safety = Path(settings.data_dir) / "backups" / body["safety_copy"]
    assert safety.is_file()

    # The app is still fully usable on the restored database.
    channels = client.get("/api/v1/channels")
    assert channels.status_code == 200

    # A newer-schema bundle is refused through the API too.
    staging_db = Path(settings.data_dir) / "future-test.db"
    conn = db_module.connect(settings.db_path)
    try:
        dst = sqlite3.connect(str(staging_db))
        try:
            conn.backup(dst)
        finally:
            dst.close()
    finally:
        conn.close()
    fconn = db_module.connect(staging_db)
    try:
        _stamp_alembic_head(fconn, "0999")
    finally:
        fconn.close()
    future_bundle = Path(settings.data_dir) / "future-test.zip"
    build_export(staging_db, future_bundle)
    try:
        refused = _post_import(client, future_bundle.read_bytes())
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert error["code"] == "backup_from_newer_app"
        assert "newer version of the app" in error["message"]
    finally:
        staging_db.unlink(missing_ok=True)
        future_bundle.unlink(missing_ok=True)
    # …and the live app still works afterwards.
    assert client.get("/api/v1/channels").status_code == 200


def test_backup_api_import_rejects_corrupt_upload(client):
    resp = _post_import(client, b"PK\x03\x04 garbage, not a real bundle")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "backup_corrupt"
