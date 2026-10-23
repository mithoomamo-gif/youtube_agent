"""Analytics RETENTION read API — API-level tests (Round 5 closure).

The retention route (``GET …/analytics/videos/{video_id}/retention``)
was the one Analytics read route the UI documented but the backend
had not landed (the UI degraded to ``endpoint_missing``).  These
tests exercise the landed route over real HTTP, following the
seeding patterns of ``test_analytics_read_api.py``:

* a stored curve with NO recorded duration returns the honest
  ``unmapped`` state: the points exactly as stored (elapsed/watch
  ratios), no section labels and no fabricated seconds;
* a stored curve WITH an explicit duration in the snapshot metrics
  is mapped by ``app.retention_sections`` (still ``unmapped`` —
  no section map is recorded — but with overall/drop stats
  computed over the real duration);
* a video with snapshots but no curve, and a not-connected
  channel, return honest ``no_data`` / ``not_connected`` states;
* cross-channel isolation: another channel's video id answers 404
  ``video_not_found``; an unknown channel answers 404
  ``channel_not_found``.
"""

from __future__ import annotations

import time
import uuid

_SUFFIX = uuid.uuid4().hex[:10]
CH_MAIN = f"ret-main-{_SUFFIX}"
CH_B = f"ret-b-{_SUFFIX}"
CH_NOTCONN = f"ret-notconn-{_SUFFIX}"
NICHE = f"ret-niche-{_SUFFIX}"
VID_CURVE = f"retV1-{_SUFFIX}"
VID_DUR = f"retV2-{_SUFFIX}"
VID_NOCURVE = f"retV3-{_SUFFIX}"
VID_B = f"retVB-{_SUFFIX}"
VID_NC = f"retVNC-{_SUFFIX}"
T0 = 1_760_000_000_000

CURVE = [
    {"elapsed_ratio": 0.0, "watch_ratio": 1.0},
    {"elapsed_ratio": 0.5, "watch_ratio": 0.6},
    {"elapsed_ratio": 1.0, "watch_ratio": 0.4},
]


def _insert_channel(conn, chan_id, yt_id, *, config=None, credential_id=None):
    import json

    now = int(time.time() * 1000)
    if credential_id is not None:
        conn.execute(
            "INSERT OR IGNORE INTO provider_credentials (id, provider,"
            " credential_kind, keyring_ref, account_label, scopes,"
            " status, last_validated_at, created_at, updated_at)"
            " VALUES (?, 'youtube', 'oauth', ?, ?, NULL, 'active', ?,"
            " ?, ?)",
            (credential_id, f"youtube:channel:{yt_id}", yt_id, now, now, now),
        )
    conn.execute(
        "INSERT OR IGNORE INTO channels (id, youtube_channel_id, title,"
        " credential_id, language, status, config_json, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, 'en', 'active', ?, ?, ?)",
        (
            chan_id,
            yt_id,
            f"Retention {chan_id}",
            credential_id,
            json.dumps(config) if config is not None else None,
            now,
            now,
        ),
    )


def _seed() -> None:
    from app import db as db_module
    from app.analytics.store import AnalyticsStore

    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO niche_profiles (id, key, label, source,"
            " created_at, updated_at) VALUES (?, ?, 'Retention Niche',"
            " 'builtin', 1, 1)",
            (NICHE, NICHE),
        )
        connected = {"connect_status": "connected",
                     "credential_status": "connected"}
        _insert_channel(conn, CH_MAIN, f"UCretMain{_SUFFIX}",
                        config=connected, credential_id=f"cred-{_SUFFIX}")
        _insert_channel(conn, CH_B, f"UCretB{_SUFFIX}xx",
                        config=connected, credential_id=f"cred-b-{_SUFFIX}")
        _insert_channel(conn, CH_NOTCONN, f"UCretNC{_SUFFIX}")
        now = int(time.time() * 1000)
        for chan, proj, vid in (
            (CH_MAIN, f"proj-r1-{_SUFFIX}", VID_CURVE),
            (CH_MAIN, f"proj-r2-{_SUFFIX}", VID_DUR),
            (CH_MAIN, f"proj-r3-{_SUFFIX}", VID_NOCURVE),
            (CH_NOTCONN, f"proj-rnc-{_SUFFIX}", VID_NC),
            (CH_B, f"proj-rb-{_SUFFIX}", VID_B),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO projects (id, channel_id,"
                " niche_profile_id, topic, topic_source, created_at,"
                " updated_at) VALUES (?, ?, ?, 'topic', 'user', ?, ?)",
                (proj, chan, NICHE, now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO publish_records (id, project_id,"
                " youtube_video_id, title, privacy_status,"
                " upload_idempotency_key, published_at, created_at)"
                " VALUES (?, ?, ?, 't', 'public', ?, ?, ?)",
                (f"pub-{proj}", proj, vid, f"idem-{proj}", T0, now),
            )
    finally:
        conn.close()

    store = AnalyticsStore(db_module.get_connection)
    store.upsert_snapshot(
        CH_MAIN, video_external_id=VID_CURVE, snapshot_kind="d7",
        metrics={"views": 100}, retention_curve=CURVE,
        captured_at_ms=T0,
    )
    store.upsert_snapshot(
        CH_MAIN, video_external_id=VID_DUR, snapshot_kind="d7",
        metrics={"views": 50, "duration_s": 200},
        retention_curve=CURVE, captured_at_ms=T0,
    )
    store.upsert_snapshot(
        CH_MAIN, video_external_id=VID_NOCURVE, snapshot_kind="d7",
        metrics={"views": 10}, captured_at_ms=T0,
    )


_SEEDED = False


def _ensure_seeded() -> None:
    global _SEEDED
    if not _SEEDED:
        _seed()
        _SEEDED = True


def test_retention_curve_without_duration_is_unmapped_ratios(client):
    _ensure_seeded()
    resp = client.get(
        f"/api/v1/channels/{CH_MAIN}/analytics/videos/{VID_CURVE}/retention"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data_state"] == "ok"
    assert body["status"] == "unmapped"
    assert body["duration_s"] is None
    assert body["sections"] == []
    assert body["points"] == CURVE
    # The nested record the UI parser reads carries the same shape.
    assert body["retention"]["status"] == "unmapped"
    assert body["retention"]["points"] == CURVE
    assert body["snapshot_kind"] == "d7"
    assert body["captured_at"] == T0
    assert "last_synced_at" in body and "sync" in body


def test_retention_curve_with_recorded_duration_is_mapped(client):
    _ensure_seeded()
    resp = client.get(
        f"/api/v1/channels/{CH_MAIN}/analytics/videos/{VID_DUR}/retention"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data_state"] == "ok"
    # No section map is recorded, so still unmapped — but the mapping
    # ran over the real duration, so overall stats exist.
    assert body["status"] == "unmapped"
    assert body["duration_s"] == 200.0
    assert body["overall"] is not None
    assert body["overall"]["entry_ratio"] == 1.0
    assert body["overall"]["exit_ratio"] == 0.4
    assert body["points"] == CURVE


def test_retention_no_curve_is_no_data(client):
    _ensure_seeded()
    resp = client.get(
        f"/api/v1/channels/{CH_MAIN}/analytics/videos/{VID_NOCURVE}/retention"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data_state"] == "no_data"
    assert body["status"] == "no_data"
    assert body["points"] == []


def test_retention_not_connected_channel(client):
    _ensure_seeded()
    resp = client.get(
        f"/api/v1/channels/{CH_NOTCONN}/analytics/videos/{VID_NC}/retention"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data_state"] == "not_connected"
    assert body["points"] == []


def test_retention_cross_channel_and_unknown_are_404(client):
    _ensure_seeded()
    other = client.get(
        f"/api/v1/channels/{CH_MAIN}/analytics/videos/{VID_B}/retention"
    )
    assert other.status_code == 404, other.text
    assert other.json()["error"]["code"] == "video_not_found"
    unknown = client.get(
        f"/api/v1/channels/nope-{_SUFFIX}/analytics/videos/{VID_CURVE}/retention"
    )
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "channel_not_found"
