"""Report endpoints (Agent 31, Round 5 W2) — receipts + weekly digest.

Covers ``app.routers.reports`` + the ``app.reports`` service contract:

- generation triggers: per-video receipt, per-channel receipt,
  weekly digest (201, stored artefact with markdown + JSON);
- the digest aggregates FIXTURE LEDGERS only: production counts,
  labelled fake-provider publish simulations, cost actuals, quota
  buckets, analytics first→last deltas, failures + a recovery, gate
  summary, and next week's planned occurrences from a real cadence;
- a channel with no analytics gets an explicit "no analytics yet"
  section, never invented zeros;
- listing with channel / scope / kind filters;
- fetch in both representations (JSON artefact, text/markdown);
- cross-channel isolation: a report of channel A is 404 under
  channel B (nested fetch, filtered top-level fetch, generation
  against a foreign video), and never appears in B's list.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

_SUFFIX = uuid.uuid4().hex[:10]
NOW = int(time.time() * 1000)
DAY = 24 * 60 * 60 * 1000
WINDOW = {"start_ms": NOW - 3 * DAY, "end_ms": NOW + DAY}

NICHE = f"rep-niche-{_SUFFIX}"
CHAN_A = f"rep-a-{_SUFFIX}"
CHAN_B = f"rep-b-{_SUFFIX}"
PROJ_A1 = f"rep-pa1-{_SUFFIX}"  # published (fake provider)
PROJ_A2 = f"rep-pa2-{_SUFFIX}"  # failed job, recovered to ready
PROJ_A3 = f"rep-pa3-{_SUFFIX}"  # failed
PROJ_B1 = f"rep-pb1-{_SUFFIX}"
SCHED_A = f"rep-sched-{_SUFFIX}"


def _seed() -> None:
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO niche_profiles (id, key, label, source,"
            " risk_class, is_active, created_at, updated_at)"
            " VALUES (?, ?, 'Facts', 'builtin', 'normal', 1, ?, ?)",
            (NICHE, f"niche-{_SUFFIX}", NOW - 10 * DAY, NOW - 10 * DAY),
        )
        for chan_id, yt_id, title in (
            (CHAN_A, f"UCa{_SUFFIX}", "Report Channel A"),
            (CHAN_B, f"UCb{_SUFFIX}", "Report Channel B"),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO channels (id, youtube_channel_id,"
                " title, default_niche_profile_id, language, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, 'ur-PK',"
                " 'active', ?, ?)",
                (chan_id, yt_id, title, NICHE, NOW - 10 * DAY, NOW - 10 * DAY),
            )

        def project(pid, chan, topic, status, created, **extra):
            conn.execute(
                "INSERT OR IGNORE INTO projects (id, channel_id,"
                " niche_profile_id, topic, topic_source, status,"
                " gate_status_json, youtube_video_id, published_at,"
                " total_cost_micros, error_summary, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pid, chan, NICHE, topic, status,
                    extra.get("gates"), extra.get("yt"), extra.get("pub_at"),
                    extra.get("cost", 0), extra.get("err"), created, created,
                ),
            )

        project(
            PROJ_A1, CHAN_A, "Ocean Facts", "published", NOW - 2 * DAY,
            gates=json.dumps({
                "gate_a": "approved",
                "gate_b": "approved",
                # Exactly what the publish finaliser merges in for a
                # fake-provider publish (provenance row of record).
                "publish": {
                    "state": "fake_provider_upload_simulated",
                    "provider": "fake",
                    "simulated": True,
                },
            }),
            yt="fake-video-1", pub_at=NOW - DAY, cost=5000,
        )
        project(PROJ_A2, CHAN_A, "Space Facts", "ready", NOW - DAY, cost=2500)
        project(
            PROJ_A3, CHAN_A, "Broken Facts", "failed", NOW - DAY,
            err="render exploded",
        )
        project(PROJ_B1, CHAN_B, "Other Channel Video", "draft", NOW - DAY)

        conn.execute(
            "INSERT OR IGNORE INTO publish_records (id, project_id,"
            " youtube_video_id, video_url, title, privacy_status,"
            " upload_idempotency_key, published_at, created_at)"
            " VALUES (?, ?, 'fake-video-1', NULL, 'Ocean Facts', 'private',"
            " ?, ?, ?)",
            (f"rep-pub-{_SUFFIX}", PROJ_A1, f"idem-{_SUFFIX}", NOW - DAY, NOW - DAY),
        )
        # Cost + quota ledger for A1.
        for i, (provider, operation, units, cost, tin, tout) in enumerate(
            (
                ("gemini", "generateContent", None, 1234, 100, 50),
                ("youtube", "videos.insert", 1, 0, 0, 0),
                ("youtube", "search.list", None, 0, 0, 0),
            )
        ):
            conn.execute(
                "INSERT OR IGNORE INTO api_usage (id, provider, operation,"
                " project_id, units, tokens_in, tokens_out, cost_micros,"
                " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)",
                (f"rep-use-{i}-{_SUFFIX}", provider, operation, PROJ_A1,
                 units, tin, tout, cost, NOW - DAY),
            )
        # Failed job for A2 (recovered: project status is 'ready').
        conn.execute(
            "INSERT OR IGNORE INTO jobs (id, project_id, agent_key, kind,"
            " status, attempts, error, error_code, created_at, updated_at)"
            " VALUES (?, ?, 'voice', 'tts', 'failed', 3, 'tts timeout',"
            " 'TTS_TIMEOUT', ?, ?)",
            (f"rep-job-{_SUFFIX}", PROJ_A2, NOW - 12 * 3600 * 1000, NOW - 12 * 3600 * 1000),
        )
        # Analytics snapshots for A1's video: first -> last delta.
        for i, (at, views, watch_x100, subs) in enumerate(
            ((NOW - 2 * DAY, 100, 1000, 3), (NOW - 6 * 3600 * 1000, 250, 1550, 2))
        ):
            conn.execute(
                "INSERT OR IGNORE INTO analytics_snapshots (id, project_id,"
                " channel_id, youtube_video_id, snapshot_at, views,"
                " watch_hours_x100, likes, comments_count, subs_delta,"
                " created_at) VALUES (?, ?, ?, 'fake-video-1', ?, ?, ?, 10,"
                " 4, ?, ?)",
                (f"rep-snap-{i}-{_SUFFIX}", PROJ_A1, CHAN_A, at, views,
                 watch_x100, subs, at),
            )
        # Daily schedule for channel A.
        conn.execute(
            "INSERT OR IGNORE INTO schedules (id, name, channel_id,"
            " niche_profile_id, cadence_json, topic_mode, is_enabled,"
            " created_at, updated_at) VALUES (?, 'Daily Facts', ?, ?, ?,"
            " 'auto', 1, ?, ?)",
            (
                SCHED_A, CHAN_A, NICHE,
                json.dumps({"type": "daily", "time": "09:00"}),
                NOW - 10 * DAY, NOW - 10 * DAY,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _seeded(client):
    """Seed after the ``client`` fixture has booted the app (and with
    it the schema); all inserts are INSERT OR IGNORE, so re-seeding
    per test is a no-op."""
    _seed()


def _generate(client, **body):
    return client.post("/api/v1/reports/generate", json=body)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def test_generate_video_receipt(client):
    resp = _generate(
        client, scope="video", kind="receipt", channel_id=CHAN_A,
        video_id=PROJ_A1, period=WINDOW,
    )
    assert resp.status_code == 201, resp.text
    artefact = resp.json()
    assert artefact["scope"] == "video"
    assert artefact["kind"] == "receipt"
    assert artefact["channel_id"] == CHAN_A
    assert artefact["project_id"] == PROJ_A1
    # Fake-provider publish is labelled a simulation, never "live"
    # (W1 engine's classification of the finaliser's provenance,
    # adapted to the W2 contract shape).
    assert artefact["data"]["publish"]["simulated"] is True
    assert artefact["data"]["publish"]["classification"] == "simulated"
    assert artefact["data"]["publish"]["state"] == "simulated_fake_provider"
    assert "NOT live on YouTube" in artefact["markdown"]
    # Ledger cost actuals flow through (api_usage in the window).
    assert artefact["data"]["costs"]["total_cost_micros"] == 1234
    assert artefact["data"]["video"]["project_total_cost_micros"] == 5000
    # The receipt really was generated by W1's engine.
    assert artefact["data"]["engine"] == "app.reporting"
    assert artefact["data"]["engine_report_id"]


def test_generate_channel_receipt(client):
    resp = _generate(
        client, scope="channel", kind="receipt", channel_id=CHAN_A,
        period=WINDOW,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert len(data["projects"]) == 3
    assert data["publishes"][0]["simulated"] is True
    assert data["quota"]["buckets"] == {"uploads": 1, "search": 1, "shared": 0}
    assert data["engine"] == "app.reporting"


def test_generate_weekly_digest_from_fixture_ledgers(client):
    resp = _generate(
        client, scope="channel", kind="weekly_digest", channel_id=CHAN_A,
        period=WINDOW,
    )
    assert resp.status_code == 201, resp.text
    artefact = resp.json()
    data = artefact["data"]
    assert data["production"]["total_created"] == 3
    assert data["production"]["by_status"] == {
        "published": 1, "ready": 1, "failed": 1,
    }
    assert data["publishing"][0]["simulated"] is True
    assert data["costs"]["total_cost_micros"] == 1234
    assert data["quota"]["buckets"]["uploads"] == 1
    assert data["quota"]["buckets"]["search"] == 1
    # Analytics deltas: 100 -> 250 views, 10.0 -> 15.5 watch hours, subs 3+2.
    assert data["performance"]["present"] is True
    assert data["performance"]["totals"]["views"] == 150
    assert data["performance"]["totals"]["watch_hours"] == 5.5
    assert data["performance"]["totals"]["subs_delta"] == 5
    # Failures + recovery of A2; A3 stays failed.
    assert len(data["failures"]["failed_jobs"]) == 1
    assert [r["project_id"] for r in data["recoveries"]] == [PROJ_A2]
    # Gate summary from gate_status_json.
    assert data["gates"] == {
        "gate_a": {"approved": 1}, "gate_b": {"approved": 1},
    }
    # Daily cadence -> exactly 7 occurrences in the coming week.
    planned = data["planned_next_week"]
    assert len(planned) == 1
    assert len(planned[0]["occurrences_in_horizon"]) == 7
    markdown = artefact["markdown"]
    for heading in (
        "## Production", "## Publishing", "## Costs (actuals)",
        "## YouTube quota used", "## Performance",
        "## Failures & recoveries", "## Gate decisions",
        "## Planned next week",
    ):
        assert heading in markdown
    assert "SIMULATED upload (fake provider" in markdown
    assert "No analytics yet" not in markdown


def test_digest_without_analytics_says_so(client):
    resp = _generate(
        client, scope="channel", kind="weekly_digest", channel_id=CHAN_B,
        period=WINDOW,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["performance"]["present"] is False
    assert "No analytics yet" in resp.json()["markdown"]
    assert data["production"]["total_created"] == 1


def test_generate_validation_errors(client):
    # Unknown channel -> 404.
    resp = _generate(
        client, scope="channel", kind="receipt",
        channel_id=f"nope-{_SUFFIX}", period=WINDOW,
    )
    assert resp.status_code == 404
    # Video scope without a video id -> 422.
    resp = _generate(client, scope="video", kind="receipt", channel_id=CHAN_A)
    assert resp.status_code == 422
    # Digest requires channel scope -> 422.
    resp = _generate(
        client, scope="video", kind="weekly_digest", channel_id=CHAN_A,
        video_id=PROJ_A1,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Listing + fetch
# ---------------------------------------------------------------------------


def test_list_reports_and_filters(client):
    _generate(
        client, scope="video", kind="receipt", channel_id=CHAN_A,
        video_id=PROJ_A1, period=WINDOW,
    )
    _generate(
        client, scope="channel", kind="weekly_digest", channel_id=CHAN_A,
        period=WINDOW,
    )
    all_items = client.get("/api/v1/reports").json()["items"]
    mine = [i for i in all_items if i["channel_id"] == CHAN_A]
    assert len(mine) >= 2
    assert all("markdown" not in i and "data" not in i for i in mine)

    by_channel = client.get(
        "/api/v1/reports", params={"channel_id": CHAN_A}
    ).json()["items"]
    assert by_channel and all(i["channel_id"] == CHAN_A for i in by_channel)

    digests = client.get(
        "/api/v1/reports",
        params={"channel_id": CHAN_A, "kind": "weekly_digest"},
    ).json()["items"]
    assert digests and all(i["kind"] == "weekly_digest" for i in digests)

    video_receipts = client.get(
        "/api/v1/reports",
        params={"channel_id": CHAN_A, "scope": "video", "kind": "receipt"},
    ).json()["items"]
    assert video_receipts and all(
        i["scope"] == "video" and i["project_id"] == PROJ_A1
        for i in video_receipts
    )

    nested = client.get(f"/api/v1/channels/{CHAN_A}/reports").json()["items"]
    assert nested and all(i["channel_id"] == CHAN_A for i in nested)


def test_fetch_both_representations(client):
    created = _generate(
        client, scope="channel", kind="weekly_digest", channel_id=CHAN_A,
        period=WINDOW,
    ).json()
    fetched = client.get(f"/api/v1/reports/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["production"]["total_created"] == 3
    assert fetched.json()["markdown"] == created["markdown"]

    md = client.get(f"/api/v1/reports/{created['id']}/markdown")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.text == created["markdown"]

    nested = client.get(f"/api/v1/channels/{CHAN_A}/reports/{created['id']}")
    assert nested.status_code == 200
    nested_md = client.get(
        f"/api/v1/channels/{CHAN_A}/reports/{created['id']}/markdown"
    )
    assert nested_md.status_code == 200
    assert nested_md.text == created["markdown"]


def test_fetch_unknown_report_404(client):
    assert client.get(f"/api/v1/reports/missing-{_SUFFIX}").status_code == 404
    assert (
        client.get(f"/api/v1/reports/missing-{_SUFFIX}/markdown").status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Cross-channel isolation
# ---------------------------------------------------------------------------


def test_cross_channel_isolation(client):
    created = _generate(
        client, scope="channel", kind="weekly_digest", channel_id=CHAN_A,
        period=WINDOW,
    ).json()
    report_id = created["id"]

    # Nested fetch under channel B -> 404 (never 403, no existence leak).
    assert (
        client.get(f"/api/v1/channels/{CHAN_B}/reports/{report_id}").status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/channels/{CHAN_B}/reports/{report_id}/markdown"
        ).status_code
        == 404
    )
    # Top-level fetch with a foreign channel filter -> 404.
    assert (
        client.get(
            f"/api/v1/reports/{report_id}", params={"channel_id": CHAN_B}
        ).status_code
        == 404
    )
    # Channel B's list never contains channel A's report.
    b_items = client.get(f"/api/v1/channels/{CHAN_B}/reports").json()["items"]
    assert all(i["id"] != report_id for i in b_items)
    assert all(i["channel_id"] == CHAN_B for i in b_items)
    # Generating a video receipt for A's video under B -> 404.
    resp = _generate(
        client, scope="video", kind="receipt", channel_id=CHAN_B,
        video_id=PROJ_A1, period=WINDOW,
    )
    assert resp.status_code == 404
