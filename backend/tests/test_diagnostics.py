"""Diagnostics bundle tests (Round 6 — 'Copy diagnostics').

Covers the four contract points:

(a) a secret seeded through the app's secret layer
    (``app.redaction.register_secret`` — the same choke point the
    config/secret layer registers real values with) and written into a
    log line appears NOWHERE in the bundle bytes, while the redaction
    marker does;
(b) no user content: a channel/project seeded with a distinctive
    title/topic string never appears — the builder only counts rows;
(c) the bundle parses, carries every required section, and its log
    lines are present but redacted (home-dir prefixes scrubbed too);
(d) the endpoint requires the launch token like every other route,
    and preview bytes are byte-identical to share bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import diagnostics
from app.config import Settings
from app.diagnostics import build_diagnostics_bundle, serialize_bundle
from app.redaction import REDACTED, register_secret

# Obviously fake — seeded ONLY through the secret layer in these tests.
FAKE_SECRET = "FAKE_SECRET_FOR_TEST_12345"
DISTINCTIVE = "DISTINCTIVE_USER_CONTENT_98765"
FIXED_NOW = "2026-10-23T00:00:00Z"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hermetic_settings(tmp_path: Path) -> Settings:
    return Settings(
        token="diag-test-token",
        boot_id="diag-boot",
        data_dir=tmp_path,
        log_level="info",
    )


def _seed_user_content(settings: Settings) -> None:
    from app import db as db_module

    conn = db_module.init_db(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO niche_profiles (id, key, label, source, created_at, updated_at)"
            " VALUES ('niche-diag-1', 'diag', 'Diag', 'builtin', 1, 1)"
        )
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language,"
            " created_at, updated_at) VALUES ('chan-diag-1', 'yt-diag-1', ?, 'en', 1, 1)",
            (DISTINCTIVE,),
        )
        conn.execute(
            "INSERT INTO projects (id, channel_id, niche_profile_id, topic,"
            " topic_source, created_at, updated_at)"
            " VALUES ('proj-diag-1', 'chan-diag-1', 'niche-diag-1', ?, 'user', 1, 1)",
            (DISTINCTIVE,),
        )
    finally:
        conn.close()


def _write_log(settings: Settings, lines: list[str]) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    (settings.log_dir / "backend.log").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# (a) seeded fake secret never leaks; redaction marker present
# ---------------------------------------------------------------------------


def test_seeded_fake_secret_absent_from_bundle_bytes(tmp_path):
    register_secret(FAKE_SECRET)
    settings = _hermetic_settings(tmp_path)
    _write_log(
        settings,
        [
            "boot ok",
            f"provider call failed using key {FAKE_SECRET} retrying",
            f"api_key={FAKE_SECRET}",
        ],
    )
    bundle = build_diagnostics_bundle(settings=settings)
    raw = serialize_bundle(bundle)
    assert FAKE_SECRET.encode() not in raw
    assert REDACTED.encode() in raw
    # The log lines themselves survive — only the secret is scrubbed.
    joined = "\n".join(bundle["logs"]["lines"])
    assert "provider call failed" in joined
    assert FAKE_SECRET not in joined


def test_fake_secret_scrubbed_from_injected_log_lines(tmp_path):
    register_secret(FAKE_SECRET)
    settings = _hermetic_settings(tmp_path)
    bundle = build_diagnostics_bundle(
        settings=settings, log_lines=[f"token is {FAKE_SECRET}"]
    )
    raw = serialize_bundle(bundle)
    assert FAKE_SECRET.encode() not in raw
    assert REDACTED in bundle["logs"]["lines"][0]


# ---------------------------------------------------------------------------
# (b) no user content
# ---------------------------------------------------------------------------


def test_no_user_content_in_bundle(tmp_path):
    settings = _hermetic_settings(tmp_path)
    _seed_user_content(settings)
    bundle = build_diagnostics_bundle(settings=settings, log_lines=[])
    raw = serialize_bundle(bundle)
    assert DISTINCTIVE.encode() not in raw
    # Counts are present (that is all the builder ever reads).
    assert bundle["counts"]["channels"] == 1
    assert bundle["counts"]["projects"] == 1
    # No content-shaped keys anywhere in the payload.
    text = raw.decode("utf-8").lower()
    for forbidden in ('"title"', '"topic"', '"script"', '"comment"', '"description"'):
        assert forbidden not in text


# ---------------------------------------------------------------------------
# (c) structure + redacted log lines + path scrubbing
# ---------------------------------------------------------------------------


def test_bundle_structure_and_required_sections(tmp_path):
    settings = _hermetic_settings(tmp_path)
    _write_log(settings, ["line one", "line two"])
    bundle = build_diagnostics_bundle(settings=settings)
    parsed = json.loads(serialize_bundle(bundle))
    assert parsed["bundle_version"] == 1
    assert parsed["generated_at"]
    for section in ("app", "runtime", "system", "health", "counts", "config_state", "logs"):
        assert section in parsed, section
    assert parsed["app"]["app_version"]
    assert parsed["app"]["backend_version"]
    assert parsed["runtime"]["python_version"]
    assert parsed["runtime"]["platform"]
    assert isinstance(parsed["system"]["cpu_count"], int)
    assert "ffmpeg" in parsed["system"]
    assert isinstance(parsed["system"]["ffmpeg"]["available"], bool)
    assert parsed["health"]["db"] in ("ok", "down", "unknown")
    assert "schema_version" in parsed["health"]
    assert "migration_head" in parsed["health"]
    assert parsed["logs"]["lines"] == ["line one", "line two"]
    assert parsed["logs"]["count"] == 2


def test_log_line_limit_takes_last_n(tmp_path):
    settings = _hermetic_settings(tmp_path)
    bundle = build_diagnostics_bundle(
        settings=settings,
        log_line_limit=2,
        log_lines=["a", "b", "c", "d"],
    )
    assert bundle["logs"]["lines"] == ["c", "d"]
    assert bundle["logs"]["limit"] == 2


def test_home_dir_prefix_scrubbed_from_log_lines(tmp_path):
    settings = _hermetic_settings(tmp_path)
    home = str(Path.home())
    bundle = build_diagnostics_bundle(
        settings=settings, log_lines=[f"wrote file {home}/private/thing.txt"]
    )
    line = bundle["logs"]["lines"][0]
    assert home not in line
    assert "~/private/thing.txt" in line
    # The absolute data dir never appears either.
    assert str(tmp_path).encode() not in serialize_bundle(bundle)


def test_serialize_is_deterministic(tmp_path):
    settings = _hermetic_settings(tmp_path)
    bundle = build_diagnostics_bundle(
        settings=settings, log_lines=["x"], generated_at=FIXED_NOW
    )
    assert serialize_bundle(bundle) == serialize_bundle(bundle)


# ---------------------------------------------------------------------------
# (d) endpoint: auth required + preview bytes == share bytes
# ---------------------------------------------------------------------------


def test_endpoint_requires_auth(app):
    with TestClient(app, base_url="http://127.0.0.1") as raw:
        resp = raw.get("/api/v1/diagnostics")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_token"


def test_endpoint_returns_parseable_bundle(client):
    resp = client.get("/api/v1/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    for section in ("app", "runtime", "system", "health", "counts", "config_state", "logs"):
        assert section in body, section
    assert body["bundle_version"] == 1


def test_preview_bytes_identical_to_share_bytes(client, monkeypatch):
    monkeypatch.setattr(diagnostics, "_utc_now_iso", lambda: FIXED_NOW)
    monkeypatch.setattr(
        diagnostics,
        "_collect_log_lines",
        lambda settings, limit: ["fixed diagnostics line"],
    )
    # Disk free space is the third volatile input: _probe_disk reads live
    # shutil.disk_usage() on every build, and any write to the volume
    # between the share and preview builds (runner logs, sqlite WAL, app
    # logs) flips disk_free_bytes by a digit — CI run #5 failed exactly
    # this way (At index 882 diff, single digit). Freeze it like the
    # other two volatile inputs above; the route-identity assertion
    # (preview == share bytes) stays byte-exact.
    monkeypatch.setattr(
        diagnostics,
        "_probe_disk",
        lambda data_dir: {"free_bytes": 123456789, "total_bytes": 987654321},
    )
    share = client.get("/api/v1/diagnostics")
    preview = client.get("/api/v1/diagnostics/preview")
    again = client.get("/api/v1/diagnostics")
    assert share.status_code == preview.status_code == again.status_code == 200
    assert preview.content == share.content
    assert again.content == share.content


def test_endpoint_redacts_seeded_secret(client, monkeypatch):
    register_secret(FAKE_SECRET)
    monkeypatch.setattr(
        diagnostics,
        "_collect_log_lines",
        lambda settings, limit: [f"leaked {FAKE_SECRET} in a log line"],
    )
    resp = client.get("/api/v1/diagnostics")
    assert resp.status_code == 200
    assert FAKE_SECRET.encode() not in resp.content
    assert REDACTED.encode() in resp.content
