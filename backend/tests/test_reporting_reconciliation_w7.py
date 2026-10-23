"""Round 5 W7 — figure reconciliation + honesty verification.

Independent audit of BOTH report engines that landed in Round 5:

* W1: ``app.reporting`` (engine / service / store, ``generated_reports``)
* W2: ``app.reports`` (receipt + weekly digest behind
  ``app.routers.reports``, ``reports`` table)

Method: seed fixture ledgers with KNOWN totals (every figure below is
hand-computed in this file from the seed data, never derived by
calling the code under test), generate the receipt reports, and
assert the JSON figures match the ledger figures EXACTLY — per-ledger
cost actuals, quota units (incl. the NULL-units classifier fallback),
per-video publish classifications, gate decisions, latest-per-video
performance totals, failures / recoveries, and budget reservations.

Publish fixtures deliberately use the REAL shape the fake-provider
publish finaliser writes (``app.execution.publish_finaliser``:
``publish_records.youtube_video_id = "simulated:<provider_id>:<pid>"``,
idempotency key ``"simulated-publish:<pid>"``, project left at
``status='ready'``) — not a hand-invented id — so a simulated publish
can never slip through labelled as a real one.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from app.reporting import service as w1_service
from app.reporting.engine import build_channel_report, build_video_report, classify_publish
from app.reporting.render import render_markdown
from tests.execution_fixtures import make_db, seed_channel_niche

DAY = 86_400_000
T0 = 1_770_000_000_000
WIN_START, WIN_END = T0, T0 + 10 * DAY


def _h() -> str:
    return uuid.uuid4().hex


def _exec(factory, sql, params=()):
    conn = factory()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixture ledgers (all expected figures are hand-computed from these rows)
# ---------------------------------------------------------------------------
#
# Channel A projects in window [T0, T0+10d):
#   P1 published, project-ledger cost 300_000
#   P2 simulated publish (real finaliser shape), cost 150_000
#   P3 publish record with a real-looking id but project never reached
#      'published' -> must classify 'unknown', cost 50_000
# Channel A project OUTSIDE the window:
#   P4 published at T0+20d, cost 999_000 (window reports must exclude it,
#      all-time reports must include it)
# Channel B (isolation contamination):
#   P5 cost 777_000 with its own usage + snapshot rows
#
# api_usage (in window unless noted):
#   P1 gemini/generateContent  cost 200_000 units NULL
#   P1 youtube/videos.insert   units 1
#   P1 youtube/search.list     units NULL  -> classifier fallback 1 (search)
#   P1 youtube/videos.list     units 3     -> shared
#   P1 elevenlabs/tts          cost 40_000, characters 900
#   P1 gemini/generateContent  cost 555_000 at T0+20d (OUT of window)
#   P2 gemini/generateContent  cost 100_000
#   P2 youtube/videos.insert   units NULL  -> classifier fallback 1 (uploads)
#   P3 youtube/thumbnails.set  units 2     -> shared (stored units win)
#   (NULL project) youtube/videos.insert units 5 -> attributable to NO
#      project; project-scoped reports must exclude it
#   P5 gemini/generateContent  cost 777_000 (channel B)
#
# agent_runs: P1 script failed(0) -> succeeded(60_000); P1 voice
#   succeeded(30_000); P2 script failed(5_000, never recovered)
# jobs: P1 job done after 3 attempts (recovery); P2 job failed
# budget_reservations: P1 active reserved 500_000 settled 200_000;
#   P1 settled reserved 100_000 settled 100_000; P2 released 80_000/0
# analytics: P1 two snapshots (latest wins), P2 one, one channel-level
#   (project NULL) snapshot for channel A, one for channel B


@pytest.fixture()
def env(tmp_path):
    db_path, factory = make_db(tmp_path)
    chan_a, niche_a = seed_channel_niche(factory)
    chan_b, niche_b = seed_channel_niche(factory)
    ids = {k: _h() for k in ("p1", "p2", "p3", "p4", "p5")}
    ids.update(chan_a=chan_a, niche_a=niche_a, chan_b=chan_b, niche_b=niche_b)
    _seed(factory, ids)
    # The engine builders take a caller-owned connection.
    conn = factory()
    yield {**ids, "db_path": db_path, "factory": factory, "conn": conn}
    conn.close()


def _project(factory, pid, chan, niche, topic, status, created, cost, gates=None,
             yt=None, pub_at=None):
    _exec(
        factory,
        "INSERT INTO projects (id, channel_id, niche_profile_id, topic,"
        " topic_source, status, gate_status_json, youtube_video_id,"
        " published_at, total_cost_micros, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?)",
        (pid, chan, niche, topic, status,
         json.dumps(gates) if gates is not None else None, yt, pub_at,
         cost, created, created),
    )


def _usage(factory, pid, provider, operation, units, cost, created, **kw):
    _exec(
        factory,
        "INSERT INTO api_usage (id, provider, operation, project_id, units,"
        " tokens_in, tokens_out, characters, cost_micros, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)",
        (_h(), provider, operation, pid, units, kw.get("tin"), kw.get("tout"),
         kw.get("chars"), cost, created),
    )


def _publish_record(factory, pid, video_id, created, pub_at=None, title="T"):
    _exec(
        factory,
        "INSERT INTO publish_records (id, project_id, youtube_video_id,"
        " video_url, title, privacy_status, upload_idempotency_key,"
        " published_at, created_at) VALUES (?, ?, ?, NULL, ?, 'private',"
        " ?, ?, ?)",
        (_h(), pid, video_id, title, f"idem-{pid}", pub_at, created),
    )


def _seed(factory, ids):
    a, na = ids["chan_a"], ids["niche_a"]
    b, nb = ids["chan_b"], ids["niche_b"]
    p1, p2, p3, p4, p5 = ids["p1"], ids["p2"], ids["p3"], ids["p4"], ids["p5"]

    _project(factory, p1, a, na, "Ocean Facts", "published", T0 + 1 * DAY,
             300_000,
             gates={"gate_a": "approved",
                    "gate_b": {"decision": "approved"},
                    "gate_b_tokens": ["qc_warn_brief_black"]},
             yt="dQw4w9WgXcQ", pub_at=T0 + 2 * DAY)
    _project(factory, p2, a, na, "Moon Facts", "ready", T0 + 2 * DAY, 150_000,
             gates={"gate_a": "approved", "gate_b": "rejected",
                    "publish": {"state": "fake_provider_upload_simulated",
                                "provider": "fake", "simulated": True}})
    _project(factory, p3, a, na, "River Facts", "ready", T0 + 3 * DAY, 50_000,
             gates={"gate_b": "pending"})
    _project(factory, p4, a, na, "Late Facts", "published", T0 + 20 * DAY,
             999_000, yt="lateVid1234", pub_at=T0 + 20 * DAY)
    _project(factory, p5, b, nb, "Other Channel", "published", T0 + 1 * DAY,
             777_000, yt="chanBVid123", pub_at=T0 + 1 * DAY)

    _publish_record(factory, p1, "dQw4w9WgXcQ", T0 + 2 * DAY,
                    pub_at=T0 + 2 * DAY, title="Ocean Facts — Final")
    # The REAL fake-provider finaliser shape (publish_finaliser.py):
    # youtube_video_id = "simulated:<provider_video_id>:<project_id>".
    _publish_record(factory, p2, f"simulated:fake-video-7:{p2}", T0 + 2 * DAY,
                    title="Moon Facts — Final")
    _exec(factory,
          "UPDATE publish_records SET upload_idempotency_key = ?"
          " WHERE project_id = ?",
          (f"simulated-publish:{p2}", p2))
    _publish_record(factory, p3, "abc123XYZ99", T0 + 3 * DAY,
                    title="River Facts — Final")
    _publish_record(factory, p4, "lateVid1234", T0 + 20 * DAY,
                    pub_at=T0 + 20 * DAY)
    _publish_record(factory, p5, "chanBVid123", T0 + 1 * DAY,
                    pub_at=T0 + 1 * DAY)

    _usage(factory, p1, "gemini", "generateContent", None, 200_000,
           T0 + 1 * DAY, tin=1000, tout=500)
    _usage(factory, p1, "youtube", "videos.insert", 1, 0, T0 + 1 * DAY)
    _usage(factory, p1, "youtube", "search.list", None, 0, T0 + 1 * DAY)
    _usage(factory, p1, "youtube", "videos.list", 3, 0, T0 + 1 * DAY)
    _usage(factory, p1, "elevenlabs", "tts", None, 40_000, T0 + 1 * DAY,
           chars=900)
    _usage(factory, p1, "gemini", "generateContent", None, 555_000,
           T0 + 20 * DAY)
    _usage(factory, p2, "gemini", "generateContent", None, 100_000,
           T0 + 2 * DAY)
    _usage(factory, p2, "youtube", "videos.insert", None, 0, T0 + 2 * DAY)
    _usage(factory, p3, "youtube", "thumbnails.set", 2, 0, T0 + 3 * DAY)
    _usage(factory, None, "youtube", "videos.insert", 5, 0, T0 + 1 * DAY)
    _usage(factory, p5, "gemini", "generateContent", None, 777_000,
           T0 + 1 * DAY)

    job1, job2 = _h(), _h()
    _exec(factory,
          "INSERT INTO jobs (id, project_id, agent_key, kind, status,"
          " attempts, created_at, updated_at) VALUES (?, ?, 'script',"
          " 'stage.script', 'done', 3, ?, ?)", (job1, p1, T0 + 1 * DAY, T0 + 1 * DAY))
    _exec(factory,
          "INSERT INTO jobs (id, project_id, agent_key, kind, status,"
          " attempts, error, created_at, updated_at) VALUES (?, ?, 'script',"
          " 'stage.script', 'failed', 1, 'boom', ?, ?)",
          (job2, p2, T0 + 2 * DAY, T0 + 2 * DAY))
    for job, pid, agent, attempt, status, cost, created in (
        (job1, p1, "script", 1, "failed", 0, T0 + 1 * DAY),
        (job1, p1, "script", 2, "succeeded", 60_000, T0 + 1 * DAY + 3_600_000),
        (job1, p1, "voice", 1, "succeeded", 30_000, T0 + 1 * DAY),
        (job2, p2, "script", 1, "failed", 5_000, T0 + 2 * DAY),
    ):
        _exec(factory,
              "INSERT INTO agent_runs (id, job_id, project_id, agent_key,"
              " attempt, status, cost_micros, created_at) VALUES"
              " (?, ?, ?, ?, ?, ?, ?, ?)",
              (_h(), job, pid, agent, attempt, status, cost, created))

    from app.execution.budget_reservations import ensure_reservations_table
    conn = factory()
    try:
        ensure_reservations_table(conn)
    finally:
        conn.close()
    for pid, status, reserved, settled in (
        (p1, "active", 500_000, 200_000),
        (p1, "settled", 100_000, 100_000),
        (p2, "released", 80_000, 0),
    ):
        _exec(factory,
              "INSERT INTO budget_reservations (id, project_id, channel_id,"
              " reserved_micros, settled_micros, status, pacific_day,"
              " pacific_month, created_at, updated_at) VALUES (?, ?, ?, ?,"
              " ?, ?, '2026-02-02', '2026-02', ?, ?)",
              (_h(), pid, a, reserved, settled, status, T0 + 1 * DAY, T0 + 1 * DAY))

    def snap(pid, chan, yt, at, views, watch, likes, comments, shares, subs):
        _exec(factory,
              "INSERT INTO analytics_snapshots (id, project_id, channel_id,"
              " youtube_video_id, snapshot_at, views, watch_hours_x100,"
              " likes, comments_count, shares, subs_delta, created_at)"
              " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (_h(), pid, chan, yt, at, views, watch, likes, comments,
               shares, subs, at))

    snap(p1, a, "dQw4w9WgXcQ", T0 + 4 * DAY, 1000, 5000, 90, 12, 3, 5)
    snap(p1, a, "dQw4w9WgXcQ", T0 + 5 * DAY, 1400, 7250, 130, 20, 5, 3)
    snap(p2, a, f"simulated:fake-video-7:{p2}", T0 + 5 * DAY, 300, 1000,
         10, 1, 0, 1)
    snap(None, a, None, T0 + 5 * DAY, 50, 100, 4, 2, 0, None)
    snap(p5, b, "chanBVid123", T0 + 5 * DAY, 9999, 9999, 999, 99, 9, 9)


# ---------------------------------------------------------------------------
# W1 engine — channel report reconciliation (windowed)
# ---------------------------------------------------------------------------


def test_w1_channel_report_reconciles_with_ledgers(env):
    report = build_channel_report(
        env["conn"], env["chan_a"],
        period_start_ms=WIN_START, period_end_ms=WIN_END,
        period_label="audit_window",
    )
    # Production: P4 (created after the window) and P5 (channel B) excluded.
    assert report["production"]["videos_produced"] == 3
    assert report["production"]["projects_by_status"] == {
        "published": 1, "ready": 2,
    }

    costs = report["costs"]
    # Project ledger: 300_000 + 150_000 + 50_000.
    assert costs["total_micros"] == 500_000
    assert costs["project_ledger_micros"] == 500_000
    assert costs["total_usd"] == pytest.approx(0.5)
    # api_usage ledger (in-window, channel-A projects only): the 555_000
    # out-of-window row, the NULL-project row and channel B's row excluded.
    assert costs["api_usage_micros"] == 340_000
    assert costs["api_usage_calls"] == 8
    by_provider = {r["provider"]: r for r in costs["by_provider"]}
    assert by_provider["gemini"]["cost_micros"] == 300_000
    assert by_provider["gemini"]["calls"] == 2
    assert by_provider["elevenlabs"]["cost_micros"] == 40_000
    assert by_provider["elevenlabs"]["characters"] == 900
    assert by_provider["youtube"]["calls"] == 5
    assert by_provider["youtube"]["cost_micros"] == 0
    # agent_runs ledger: 60_000 + 30_000 + 5_000 (+ one 0-cost failure).
    assert costs["agent_runs_micros"] == 95_000
    by_agent = {r["agent_key"]: r for r in costs["by_agent"]}
    assert by_agent["script"]["cost_micros"] == 65_000
    assert by_agent["script"]["runs"] == 3
    assert by_agent["voice"]["cost_micros"] == 30_000
    # Reservations: remaining counts ACTIVE rows only (500k-200k);
    # settled sums every row's settled_micros (200k + 100k + 0).
    assert costs["reserved_micros"] == 300_000
    assert costs["settled_micros"] == 300_000
    assert len(costs["reservations"]) == 3
    # Ledgers are reported per-ledger, never cross-summed into the headline.
    assert costs["total_micros"] != (
        costs["api_usage_micros"] + costs["agent_runs_micros"]
    )

    # Quota (June-2026 buckets; stored units win, NULL -> classifier):
    # uploads 1 (stored) + 1 (fallback) = 2; search 1 (fallback);
    # shared 3 (videos.list) + 2 (thumbnails.set) = 5.
    quota = report["quota"]
    assert quota["total_units"] == 8
    assert quota["calls"] == 5
    assert quota["buckets"]["uploads"] == {"used": 2, "limit": 100}
    assert quota["buckets"]["search"] == {"used": 1, "limit": 100}
    assert quota["buckets"]["shared"] == {"used": 5, "limit": 10000}

    # Publish classifications per video.
    publish = report["publish"]
    assert publish["counts"] == {
        "published": 1, "simulated": 1, "unknown": 1,
    }
    assert publish["simulated"] is True
    per_video = {v["project_id"]: v for v in report["production"]["videos"]}
    assert per_video[env["p1"]]["publish_classification"] == "published"
    assert per_video[env["p2"]]["publish_classification"] == "simulated"
    assert per_video[env["p2"]]["simulated"] is True
    assert per_video[env["p3"]]["publish_classification"] == "unknown"

    # Gate outcome counts across the period's videos.
    counts = report["gates"]["counts"]
    assert counts["gate_a"] == {"passed": 2, "not_recorded": 1}
    assert counts["gate_b"] == {"passed": 1, "failed": 1, "pending": 1}
    assert counts["gate_c"] == {"passed": 3}

    # Performance: latest snapshot per video only (P1's older snapshot
    # must not double-count); channel-level snapshot included as its
    # own row per the engine's documented scope.
    perf = report["performance"]
    assert perf["available"] is True
    assert perf["snapshot_count"] == 4
    assert perf["videos_with_analytics"] == 3
    totals = perf["totals_from_latest_per_video"]
    assert totals["views"] == 1400 + 300 + 50
    assert totals["likes"] == 130 + 10 + 4
    assert totals["comments_count"] == 20 + 1 + 2
    assert totals["shares"] == 5
    assert totals["watch_hours"] == pytest.approx(72.5 + 10.0 + 1.0)
    assert totals["subs_delta"] == 3 + 1 + 0

    # Failures & recoveries: 1 failed job + 2 failed agent runs;
    # recoveries = the retried-then-done job + P1's script agent.
    failures = report["failures_recoveries"]
    assert failures["failed_jobs"] == 1
    assert failures["failed_agent_runs"] == 2
    assert failures["failure_count"] == 3
    assert failures["recovery_count"] == 2


def test_w1_channel_report_all_time_includes_late_project(env):
    report = build_channel_report(env["conn"], env["chan_a"])
    assert report["production"]["videos_produced"] == 4
    assert report["costs"]["total_micros"] == 500_000 + 999_000
    # All-time api_usage now includes P1's late 555_000 row.
    assert report["costs"]["api_usage_micros"] == 340_000 + 555_000
    assert report["costs"]["api_usage_calls"] == 9
    assert report["publish"]["counts"] == {
        "published": 2, "simulated": 1, "unknown": 1,
    }


def test_w1_channel_report_cross_channel_isolation(env):
    report_b = build_channel_report(
        env["conn"], env["chan_b"],
        period_start_ms=WIN_START, period_end_ms=WIN_END,
    )
    assert report_b["production"]["videos_produced"] == 1
    assert report_b["costs"]["total_micros"] == 777_000
    assert report_b["costs"]["api_usage_micros"] == 777_000
    assert report_b["performance"]["totals_from_latest_per_video"]["views"] == 9999


# ---------------------------------------------------------------------------
# W1 engine — video reports
# ---------------------------------------------------------------------------


def test_w1_video_report_p1_lifetime_figures(env):
    report = build_video_report(env["conn"], env["p1"])
    assert report["publish"]["classification"] == "published"
    assert report["publish"]["simulated"] is False
    assert report["publish"]["youtube_video_id"] == "dQw4w9WgXcQ"
    assert report["costs"]["total_micros"] == 300_000
    # Lifetime scope reads ALL of the project's usage rows (incl. late).
    assert report["costs"]["api_usage_micros"] == 240_000 + 555_000
    assert report["costs"]["api_usage_calls"] == 6
    assert report["quota"]["total_units"] == 1 + 1 + 3
    assert report["quota"]["buckets"]["uploads"]["used"] == 1
    assert report["quota"]["buckets"]["search"]["used"] == 1
    assert report["quota"]["buckets"]["shared"]["used"] == 3
    gates = report["gates"]
    assert gates["gate_a"]["outcome"] == "passed"
    assert gates["gate_b"]["outcome"] == "passed"
    assert gates["gate_b"]["tokens"] == ["qc_warn_brief_black"]
    assert gates["gate_c"]["outcome"] == "passed"
    perf = report["performance"]
    assert perf["snapshot_count"] == 2
    assert perf["totals_from_latest_per_video"]["views"] == 1400


def test_w1_video_report_p1_windowed_excludes_late_usage(env):
    report = build_video_report(
        env["conn"], env["p1"],
        period_start_ms=WIN_START, period_end_ms=WIN_END,
    )
    assert report["costs"]["api_usage_micros"] == 240_000
    assert report["costs"]["api_usage_calls"] == 5


def test_w1_video_report_simulated_publish_labelled(env):
    report = build_video_report(env["conn"], env["p2"])
    publish = report["publish"]
    assert publish["classification"] == "simulated"
    assert publish["simulated"] is True
    assert "not published to YouTube" in publish["label"]
    assert publish["youtube_video_id"] == f"simulated:fake-video-7:{env['p2']}"
    markdown = render_markdown(report)
    assert "**simulated** publish via the fake provider" in markdown
    assert "No video was uploaded to YouTube" in markdown


def test_w1_video_report_unconfirmed_record_is_unknown(env):
    report = build_video_report(env["conn"], env["p3"])
    assert report["publish"]["classification"] == "unknown"
    assert report["publish"]["label"] != "Published"
    assert report["gates"]["gate_b"]["outcome"] == "pending"
    assert report["gates"]["gate_a"]["outcome"] == "not_recorded"


def test_w1_markdown_figures_match_json(env):
    report = build_channel_report(
        env["conn"], env["chan_a"],
        period_start_ms=WIN_START, period_end_ms=WIN_END,
    )
    markdown = render_markdown(report)
    # _fmt_usd renders "(<micros> micros)" — the exact ledger integers
    # from the JSON must appear in the Markdown rendering.
    assert "(500000 micros)" in markdown
    assert "(340000 micros)" in markdown
    assert "(95000 micros)" in markdown
    assert "Total units: **8**" in markdown


def test_w1_service_round_trip_and_list_isolation(env):
    generated = w1_service.generate_video_report(
        env["factory"], env["p2"], reports_dir=None, persist=True,
    )
    fetched = w1_service.get_report(env["factory"], generated["report_id"])
    assert fetched is not None
    assert fetched["report"]["publish"]["classification"] == "simulated"
    assert fetched["report"]["costs"]["total_micros"] == 150_000
    assert fetched["markdown"] == render_markdown(generated)

    mine = w1_service.list_reports(env["factory"], channel_id=env["chan_a"])
    assert [m["report_id"] for m in mine] == [generated["report_id"]]
    other = w1_service.list_reports(env["factory"], channel_id=env["chan_b"])
    assert other == []


# ---------------------------------------------------------------------------
# classify_publish — adversarial matrix (pure function)
# ---------------------------------------------------------------------------


def test_classify_publish_simulated_evidence_always_wins():
    project = {"status": "published", "youtube_video_id": "realVid12345",
               "gate_status_json": json.dumps(
                   {"publish": {"simulated": True, "provider": "fake"}})}
    record = {"id": "r1", "youtube_video_id": "realVid12345"}
    result = classify_publish(project, record)
    assert result["classification"] == "simulated"


def test_classify_publish_simulated_id_prefix_wins_over_published_status():
    project = {"status": "published", "gate_status_json": None}
    record = {"id": "r1", "youtube_video_id": "simulated:fake-video-1:proj-x"}
    result = classify_publish(project, record)
    assert result["classification"] == "simulated"
    assert result["simulated"] is True


def test_classify_publish_no_record_but_simulated_state():
    project = {"status": "ready",
               "gate_status_json": json.dumps(
                   {"publish": {"state": "fake_provider_upload_simulated"}})}
    result = classify_publish(project, None)
    assert result["classification"] == "simulated"


def test_classify_publish_real_and_unrecorded():
    project = {"status": "published", "gate_status_json": None}
    record = {"id": "r1", "youtube_video_id": "dQw4w9WgXcQ"}
    assert classify_publish(project, record)["classification"] == "published"
    assert classify_publish(project, None)["classification"] == "not_published"
    project_ready = {"status": "ready", "gate_status_json": None}
    assert classify_publish(project_ready, record)["classification"] == "unknown"


# ---------------------------------------------------------------------------
# W2 (app.reports) — HTTP round-trips against the REAL finaliser shape
# ---------------------------------------------------------------------------
#
# The shared session DB is used here (the service layer opens the app
# DB itself), with unique ids and a HISTORICAL window so no other
# module's rows can pollute the figures.  The key honesty case: the
# publish record carries the id the real publish finaliser writes —
# "simulated:fake-video-9:<project_id>" — and every W2 representation
# (video receipt, channel receipt, weekly digest; JSON + Markdown)
# must label it simulated, never a published record.

_SUFFIX = uuid.uuid4().hex[:10]
W2_CHAN = f"w7-chan-{_SUFFIX}"
W2_CHAN_B = f"w7-chanb-{_SUFFIX}"
W2_NICHE = f"w7-niche-{_SUFFIX}"
W2_PROJ = f"w7-proj-{_SUFFIX}"
W2_SIM_ID = f"simulated:fake-video-9:{W2_PROJ}"
W2_WIN = {"start_ms": T0, "end_ms": T0 + 10 * DAY}


def _w2_seed() -> None:
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO niche_profiles (id, key, label, source,"
            " risk_class, is_active, created_at, updated_at) VALUES"
            " (?, ?, 'Facts', 'builtin', 'normal', 1, ?, ?)",
            (W2_NICHE, f"w7-niche-key-{_SUFFIX}", T0, T0),
        )
        for chan, yt in ((W2_CHAN, f"UCw7a{_SUFFIX}"), (W2_CHAN_B, f"UCw7b{_SUFFIX}")):
            conn.execute(
                "INSERT OR IGNORE INTO channels (id, youtube_channel_id,"
                " title, default_niche_profile_id, language, status,"
                " created_at, updated_at) VALUES (?, ?, 'W7 Channel', ?,"
                " 'en-US', 'active', ?, ?)",
                (chan, yt, W2_NICHE, T0, T0),
            )
        conn.execute(
            "INSERT OR IGNORE INTO projects (id, channel_id,"
            " niche_profile_id, topic, topic_source, status,"
            " gate_status_json, total_cost_micros, created_at, updated_at)"
            " VALUES (?, ?, ?, 'W7 Moon Facts', 'user', 'ready', ?, 4242,"
            " ?, ?)",
            (W2_PROJ, W2_CHAN, W2_NICHE,
             json.dumps({"gate_a": "approved",
                         "publish": {
                             "state": "fake_provider_upload_simulated",
                             "provider": "fake", "simulated": True}}),
             T0 + DAY, T0 + DAY),
        )
        conn.execute(
            "INSERT OR IGNORE INTO publish_records (id, project_id,"
            " youtube_video_id, video_url, title, privacy_status,"
            " upload_idempotency_key, published_at, created_at) VALUES"
            " (?, ?, ?, NULL, 'W7 Moon Facts', 'private', ?, NULL, ?)",
            (f"w7-pub-{_SUFFIX}", W2_PROJ, W2_SIM_ID,
             f"simulated-publish:{W2_PROJ}", T0 + DAY),
        )
        for i, (provider, operation, units, cost) in enumerate(
            (("gemini", "generateContent", None, 4242),
             ("youtube", "videos.insert", None, 0))
        ):
            conn.execute(
                "INSERT OR IGNORE INTO api_usage (id, provider, operation,"
                " project_id, units, cost_micros, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'ok', ?)",
                (f"w7-use-{i}-{_SUFFIX}", provider, operation, W2_PROJ,
                 units, cost, T0 + DAY),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def w2_client(client):
    _w2_seed()
    return client


def test_w2_video_receipt_labels_real_finaliser_simulation(w2_client):
    resp = w2_client.post(
        "/api/v1/reports/generate",
        json={"scope": "video", "kind": "receipt", "channel_id": W2_CHAN,
              "video_id": W2_PROJ, "period": W2_WIN},
    )
    assert resp.status_code == 201, resp.text
    artefact = resp.json()
    publish = artefact["data"]["publish"]
    assert publish["simulated"] is True
    assert publish["state"] == "simulated_fake_provider"
    assert "NOT live on YouTube" in artefact["markdown"]
    assert "Published record:" not in artefact["markdown"]
    # Ledger figures reconcile through the HTTP layer.
    assert artefact["data"]["costs"]["total_cost_micros"] == 4242
    assert artefact["data"]["video"]["project_total_cost_micros"] == 4242


def test_w2_channel_receipt_and_digest_label_simulation(w2_client):
    receipt = w2_client.post(
        "/api/v1/reports/generate",
        json={"scope": "channel", "kind": "receipt", "channel_id": W2_CHAN,
              "period": W2_WIN},
    ).json()
    assert receipt["data"]["publishes"][0]["youtube_video_id"] == W2_SIM_ID
    assert receipt["data"]["publishes"][0]["simulated"] is True
    assert "SIMULATED (fake provider" in receipt["markdown"]
    # NULL-units videos.insert falls back to the classifier's 1 unit.
    assert receipt["data"]["quota"]["buckets"]["uploads"] == 1
    assert receipt["data"]["costs"]["total_cost_micros"] == 4242

    digest = w2_client.post(
        "/api/v1/reports/generate",
        json={"scope": "channel", "kind": "weekly_digest",
              "channel_id": W2_CHAN, "period": W2_WIN},
    ).json()
    assert digest["data"]["publishing"][0]["simulated"] is True
    assert "SIMULATED upload (fake provider" in digest["markdown"]
    assert digest["data"]["costs"]["total_cost_micros"] == 4242
    assert digest["data"]["project_costs_total_micros"] == 4242
    # No analytics snapshots for this channel: honest absence.
    assert digest["data"]["performance"]["present"] is False
    assert "No analytics yet" in digest["markdown"]


def test_w2_http_round_trip_fetch_equals_generated(w2_client):
    created = w2_client.post(
        "/api/v1/reports/generate",
        json={"scope": "video", "kind": "receipt", "channel_id": W2_CHAN,
              "video_id": W2_PROJ, "period": W2_WIN},
    ).json()
    fetched = w2_client.get(f"/api/v1/reports/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"] == created["data"]
    assert fetched.json()["markdown"] == created["markdown"]
    md = w2_client.get(f"/api/v1/reports/{created['id']}/markdown")
    assert md.status_code == 200
    assert md.text == created["markdown"]
    # Cross-channel: nested fetch under channel B is 404, and B's list
    # never contains the report.
    assert w2_client.get(
        f"/api/v1/channels/{W2_CHAN_B}/reports/{created['id']}"
    ).status_code == 404
    b_items = w2_client.get(f"/api/v1/channels/{W2_CHAN_B}/reports").json()["items"]
    assert all(i["id"] != created["id"] for i in b_items)
    a_items = w2_client.get(f"/api/v1/channels/{W2_CHAN}/reports").json()["items"]
    assert any(i["id"] == created["id"] for i in a_items)


def test_w2_real_publish_record_not_labelled_simulated(w2_client):
    # Guard against overcorrection: a record with a genuine-looking
    # YouTube id (no simulated evidence anywhere) stays un-simulated.
    from app.reports.facts import is_simulated_publish

    assert is_simulated_publish({"youtube_video_id": "dQw4w9WgXcQ"}) is False
    assert is_simulated_publish({"youtube_video_id": W2_SIM_ID}) is True
    assert is_simulated_publish({"youtube_video_id": "fake-video-1"}) is True
