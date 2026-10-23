"""Reporting engine tests (Round 5, W1) — ``app.reporting``.

The engine's contract under test:

* every figure comes from a ledger / record table (seeded here
  through the real section-15 schema via ``tests.execution_fixtures``);
* a fake-provider simulated publish is ALWAYS classified
  ``simulated`` and labelled as not published to YouTube;
* no analytics snapshots -> an explicit "no analytics yet" section,
  never invented metrics;
* no calibration seam -> ``calibration_error.available`` is False;
  a ``calibration_errors`` ledger row IS consumed when present;
* a project / channel with no ledger rows still generates, with
  honest empty sections;
* generated reports persist as artefacts (DB row with full JSON +
  Markdown, optional file copies) and list / fetch through the
  service seam W2 builds on.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from app.reporting import service
from app.reporting.engine import ReportNotFoundError
from app.reporting.render import render_markdown
from tests.execution_fixtures import make_db, seed_channel_niche, seed_project

T0 = 1_760_000_000_000  # fixed epoch-ms base for seeded rows


def _ms(offset: int = 0) -> int:
    return T0 + offset


def _exec(factory, sql, params=()):
    conn = factory()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def env(tmp_path):
    db_path, factory = make_db(tmp_path)
    channel_id, niche_id = seed_channel_niche(factory)
    return {
        "db_path": db_path, "factory": factory,
        "channel_id": channel_id, "niche_id": niche_id,
        "reports_dir": tmp_path / "reports",
    }


def _rich_project(env) -> str:
    """One project with a full set of ledger rows; returns its id."""
    factory, ch, niche = env["factory"], env["channel_id"], env["niche_id"]
    pid = seed_project(factory, ch, niche, topic="Ocean Facts", status="ready")
    _exec(factory,
          "UPDATE projects SET total_cost_micros = 125000,"
          " gate_status_json = ?, target_minutes = 3 WHERE id = ?",
          (json.dumps({"gate_a": "approved", "gate_b": "approved",
                       "publish": {"state": "fake_provider_upload_simulated",
                                   "provider": "fake", "simulated": True}}), pid))
    # Artefacts.
    for artefact_type, version in (("script", 1), ("master_video", 1), ("thumbnail", 2)):
        _exec(factory,
              "INSERT INTO project_artefacts (id, project_id, artefact_type,"
              " version, file_path, content_hash, is_locked, created_at)"
              " VALUES (?, ?, ?, ?, ?, 'hash', 0, ?)",
              (uuid.uuid4().hex, pid, artefact_type, version,
               f"/pack/{artefact_type}.bin", _ms()))
    # Assets + licence.
    asset_id = uuid.uuid4().hex
    _exec(factory,
          "INSERT INTO assets (id, content_hash, asset_type, file_path,"
          " source_provider, created_at) VALUES (?, 'ahash', 'stock_video',"
          " '/assets/a.mp4', 'pixabay', ?)", (asset_id, _ms()))
    _exec(factory,
          "INSERT INTO asset_licences (asset_id, licence_type, commercial_use_ok)"
          " VALUES (?, 'pixabay_content', 1)", (asset_id,))
    _exec(factory,
          "INSERT INTO project_assets (project_id, asset_id, scene_no,"
          " usage_role, created_at) VALUES (?, ?, 1, 'scene_visual', ?)",
          (pid, asset_id, _ms()))
    # Jobs (one failed, one recovered after 3 attempts) + agent runs.
    failed_job = uuid.uuid4().hex
    _exec(factory,
          "INSERT INTO jobs (id, project_id, agent_key, kind, status, attempts,"
          " error, created_at, updated_at) VALUES (?, ?, 'script', 'stage.script',"
          " 'failed', 1, 'boom', ?, ?)", (failed_job, pid, _ms(1), _ms(2)))
    ok_job = uuid.uuid4().hex
    _exec(factory,
          "INSERT INTO jobs (id, project_id, agent_key, kind, status, attempts,"
          " created_at, updated_at) VALUES (?, ?, 'voice', 'stage.voice',"
          " 'done', 3, ?, ?)", (ok_job, pid, _ms(1), _ms(3)))
    for status, attempt, cost in (("failed", 1, 0), ("succeeded", 2, 50000)):
        _exec(factory,
              "INSERT INTO agent_runs (id, job_id, project_id, agent_key, attempt,"
              " status, cost_micros, error, created_at) VALUES (?, ?, ?, 'script',"
              " ?, ?, ?, ?, ?)",
              (uuid.uuid4().hex, failed_job if status == "failed" else ok_job,
               pid, attempt, status, cost,
               "llm timeout" if status == "failed" else None, _ms(attempt)))
    # api_usage: LLM spend + YouTube quota (one row with NULL units).
    for provider, operation, units, cost in (
        ("gemini", "generateContent", None, 75000),
        ("youtube", "videos.insert", 1600, 0),
        ("youtube", "search.list", None, 0),
        ("youtube", "videos.list", 1, 0),
    ):
        _exec(factory,
              "INSERT INTO api_usage (id, provider, operation, project_id, units,"
              " tokens_in, tokens_out, cost_micros, status, created_at)"
              " VALUES (?, ?, ?, ?, ?, 100, 50, ?, 'ok', ?)",
              (uuid.uuid4().hex, provider, operation, pid, units, cost, _ms(5)))
    # Simulated publish record (the publish-finaliser's namespaced id).
    _exec(factory,
          "INSERT INTO publish_records (id, project_id, youtube_video_id,"
          " video_url, title, privacy_status, disclosure_altered_content,"
          " upload_idempotency_key, published_at, created_at) VALUES (?, ?, ?,"
          " NULL, 'Ocean Facts — Final', 'private', 1, ?, NULL, ?)",
          (uuid.uuid4().hex, pid, f"simulated:fake-video-1:{pid}",
           f"simulated-publish:{pid}", _ms(6)))
    # Analytics: two snapshots, latest must win for totals.
    for snap_at, views in ((_ms(10), 100), (_ms(20), 250)):
        _exec(factory,
              "INSERT INTO analytics_snapshots (id, project_id, channel_id,"
              " youtube_video_id, snapshot_at, views, watch_hours_x100, likes,"
              " comments_count, created_at) VALUES (?, ?, ?, 'realvid1', ?, ?,"
              " 1250, 40, 7, ?)",
              (uuid.uuid4().hex, pid, ch, snap_at, views, snap_at))
    # Schedule + pending topic (next planned work).
    sched_id = uuid.uuid4().hex
    _exec(factory,
          "INSERT INTO schedules (id, name, channel_id, niche_profile_id,"
          " topic_mode, publish_mode, is_enabled, next_run_at, created_at,"
          " updated_at) VALUES (?, 'Daily Facts', ?, ?, 'auto', 'review_first',"
          " 1, ?, ?, ?)", (sched_id, ch, niche, _ms(100_000), _ms(), _ms()))
    _exec(factory,
          "INSERT INTO topic_queue (id, channel_id, niche_profile_id, topic,"
          " source, status, created_at) VALUES (?, ?, ?, 'Next Ocean Topic',"
          " 'user', 'pending', ?)", (uuid.uuid4().hex, ch, niche, _ms()))
    _exec(factory, "UPDATE projects SET schedule_id = ? WHERE id = ?",
          (sched_id, pid))
    return pid


# ---------------------------------------------------------------------------
# Video report — full ledger figures
# ---------------------------------------------------------------------------


def test_video_report_reads_every_ledger(env):
    pid = _rich_project(env)
    report = service.build_video_report(env["factory"], pid)

    assert report["scope"] == "video"
    assert report["channel_id"] == env["channel_id"]
    assert report["production"]["artefact_count"] == 3
    assert report["production"]["assets"]["asset_count"] == 1
    assert report["production"]["assets"]["licence_count"] == 1

    costs = report["costs"]
    assert costs["total_micros"] == 125000  # projects ledger headline
    assert costs["api_usage_micros"] == 75000
    assert costs["agent_runs_micros"] == 50000
    providers = {r["provider"]: r for r in costs["by_provider"]}
    assert providers["gemini"]["cost_micros"] == 75000
    assert providers["youtube"]["calls"] == 3

    quota = report["quota"]
    assert quota["calls"] == 3
    # videos.insert stored 1600; search.list NULL -> classify fallback 1;
    # videos.list stored 1.
    assert quota["total_units"] == 1602
    assert quota["buckets"]["uploads"]["used"] == 1600
    assert quota["buckets"]["search"]["used"] == 1
    assert quota["buckets"]["shared"]["used"] == 1

    gates = report["gates"]
    assert gates["gate_a"]["outcome"] == "passed"
    assert gates["gate_b"]["outcome"] == "passed"
    # Publish gate is ledger-implied by the publish record.
    assert gates["gate_c"]["outcome"] == "passed"

    perf = report["performance"]
    assert perf["available"] is True
    assert perf["snapshot_count"] == 2
    assert perf["totals_from_latest_per_video"]["views"] == 250  # not 350

    fail = report["failures_recoveries"]
    assert fail["failed_jobs"] == 1
    assert fail["failed_agent_runs"] == 1
    assert fail["failure_count"] == 2
    # Job recovered after 3 attempts + agent failed-then-succeeded.
    assert fail["recovery_count"] == 2

    nxt = report["next_planned"]
    assert nxt["enabled_schedule_count"] == 1
    assert nxt["pending_topic_count"] == 1
    assert nxt["pending_topics"][0]["topic"] == "Next Ocean Topic"


def test_video_report_simulated_publish_never_called_published(env):
    pid = _rich_project(env)
    report = service.build_video_report(env["factory"], pid)
    publish = report["publish"]
    assert publish["classification"] == "simulated"
    assert publish["simulated"] is True
    assert "not published" in publish["label"].lower()
    md = render_markdown(report)
    assert "Simulated publish (fake provider)" in md
    assert "No video was uploaded to YouTube" in md


def test_video_report_markdown_matches_json_figures(env):
    pid = _rich_project(env)
    report = service.build_video_report(env["factory"], pid)
    md = render_markdown(report)
    assert "$0.125000 (125000 micros)" in md  # project-ledger total
    assert "Total units: **1602**" in md
    assert "views 250" in md


def test_real_publish_classification(env):
    factory, ch, niche = env["factory"], env["channel_id"], env["niche_id"]
    pid = seed_project(factory, ch, niche, topic="Real One", status="published")
    _exec(factory,
          "INSERT INTO publish_records (id, project_id, youtube_video_id,"
          " video_url, title, privacy_status, upload_idempotency_key,"
          " published_at, created_at) VALUES (?, ?, 'dQw4w9WgXcQ',"
          " 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'Real One',"
          " 'public', 'key-real', ?, ?)",
          (uuid.uuid4().hex, pid, _ms(50), _ms(50)))
    report = service.build_video_report(factory, pid)
    assert report["publish"]["classification"] == "published"
    assert report["publish"]["simulated"] is False


def test_unconfirmed_publish_record_is_unknown_not_published(env):
    factory, ch, niche = env["factory"], env["channel_id"], env["niche_id"]
    pid = seed_project(factory, ch, niche, topic="Half", status="ready")
    _exec(factory,
          "INSERT INTO publish_records (id, project_id, youtube_video_id,"
          " title, privacy_status, upload_idempotency_key, created_at)"
          " VALUES (?, ?, 'abc123XYZ', 'Half', 'private', 'key-half', ?)",
          (uuid.uuid4().hex, pid, _ms()))
    report = service.build_video_report(factory, pid)
    assert report["publish"]["classification"] == "unknown"


# ---------------------------------------------------------------------------
# Honest empties
# ---------------------------------------------------------------------------


def test_empty_project_report_is_honest(env):
    factory, ch, niche = env["factory"], env["channel_id"], env["niche_id"]
    pid = seed_project(factory, ch, niche, topic="Bare")
    report = service.build_video_report(factory, pid)
    assert report["costs"]["total_micros"] == 0
    assert report["quota"]["total_units"] == 0
    assert report["publish"]["classification"] == "not_published"
    assert report["performance"]["available"] is False
    assert "no analytics yet" in report["performance"]["reason"]
    assert report["calibration_error"]["available"] is False
    assert report["gates"]["gate_a"]["outcome"] == "not_recorded"
    assert report["gates"]["gate_b"]["outcome"] == "not_recorded"
    assert report["gates"]["gate_c"]["outcome"] == "not_recorded"
    assert report["failures_recoveries"]["failure_count"] == 0
    md = render_markdown(report)
    assert "No analytics yet" in md
    assert "Not available" in md


def test_unknown_project_and_channel_raise(env):
    with pytest.raises(ReportNotFoundError):
        service.build_video_report(env["factory"], "no-such-project")
    with pytest.raises(ReportNotFoundError):
        service.build_channel_report(env["factory"], "no-such-channel")


def test_calibration_errors_ledger_is_consumed_when_present(env):
    factory, ch, niche = env["factory"], env["channel_id"], env["niche_id"]
    pid = seed_project(factory, ch, niche, topic="Calibrated")
    conn = factory()
    try:
        conn.execute(
            "CREATE TABLE calibration_errors (id TEXT PRIMARY KEY,"
            " channel_id TEXT, project_id TEXT, niche TEXT, value REAL,"
            " unit TEXT, created_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO calibration_errors VALUES ('c1', ?, ?, 'facts',"
            " 0.042, 'duration_ratio', ?)", (ch, pid, _ms()))
        conn.commit()
    finally:
        conn.close()
    report = service.build_video_report(factory, pid)
    calib = report["calibration_error"]
    assert calib["available"] is True
    assert calib["value"] == pytest.approx(0.042)
    assert calib["unit"] == "duration_ratio"
    assert calib["source"] == "calibration_errors ledger"


def test_real_calibration_seam_is_consumed(env):
    """The landed app.calibration read model (calibration_log ledger)
    feeds the report's calibration section verbatim per scope."""
    factory, ch, niche = env["factory"], env["channel_id"], env["niche_id"]
    # One calibration row per video (the log's uniqueness rule), so
    # the scope's evidence comes from three graded videos.
    pids = [seed_project(factory, ch, niche, topic=f"Graded {i}")
            for i in range(3)]
    pid = pids[0]
    conn = factory()
    try:
        from app.calibration.schema import ensure_calibration_schema

        ensure_calibration_schema(conn)
        for i, (proj_id, actual) in enumerate(
                zip(pids, (110.0, 90.0, 100.0))):
            conn.execute(
                "INSERT INTO calibration_log (id, project_id, channel_id,"
                " niche, metric, snapshot_point, predicted_low, predicted_mid,"
                " predicted_high, actual_value, created_at) VALUES (?, ?, ?,"
                " 'facts', 'views', 'D+7', 80, 100, 120, ?, ?)",
                (uuid.uuid4().hex, proj_id, ch, actual, _ms(i)))
        conn.commit()
    finally:
        conn.close()

    report = service.build_video_report(factory, pid)
    calib = report["calibration_error"]
    assert calib["available"] is True
    assert calib["source"] == "app.calibration (calibration_log)"
    assert calib["value"] is None  # never pooled across metrics
    scopes = calib["detail"]["scopes"]
    assert len(scopes) == 1
    point = scopes[0]["points"]["D+7"]
    assert point["n"] == 3
    assert point["mape_pct"] == pytest.approx(20.0 / 3.0)
    assert point["band_coverage_pct"] == pytest.approx(100.0)
    assert len(calib["detail"]["video_rows"]) == 1  # this video's own row
    md = render_markdown(report)
    assert "MAPE 6.67%" in md

    channel_report = service.build_channel_report(factory, ch)
    assert channel_report["calibration_error"]["available"] is True


def test_calibration_channel_data_but_not_this_video(env):
    factory, ch, niche = env["factory"], env["channel_id"], env["niche_id"]
    graded = seed_project(factory, ch, niche, topic="Graded")
    other = seed_project(factory, ch, niche, topic="Ungraded")
    conn = factory()
    try:
        from app.calibration.schema import ensure_calibration_schema

        ensure_calibration_schema(conn)
        conn.execute(
            "INSERT INTO calibration_log (id, project_id, channel_id, niche,"
            " metric, snapshot_point, predicted_low, predicted_mid,"
            " predicted_high, actual_value, created_at) VALUES (?, ?, ?,"
            " 'facts', 'views', 'D+7', 80, 100, 120, 100, ?)",
            (uuid.uuid4().hex, graded, ch, _ms()))
        conn.commit()
    finally:
        conn.close()
    report = service.build_video_report(factory, other)
    calib = report["calibration_error"]
    assert calib["available"] is False
    assert "none grade this project" in calib["reason"]


# ---------------------------------------------------------------------------
# Channel report
# ---------------------------------------------------------------------------


def test_channel_report_aggregates_and_latest_per_video(env):
    pid = _rich_project(env)
    factory, ch, niche = env["factory"], env["channel_id"], env["niche_id"]
    pid2 = seed_project(factory, ch, niche, topic="Second", status="draft")
    _exec(factory, "UPDATE projects SET total_cost_micros = 5000 WHERE id = ?",
          (pid2,))

    report = service.build_channel_report(factory, ch)
    assert report["production"]["videos_produced"] == 2
    assert report["costs"]["total_micros"] == 130000
    assert report["publish"]["counts"] == {"simulated": 1, "not_published": 1}
    assert report["publish"]["simulated"] is True
    assert report["gates"]["counts"]["gate_a"]["passed"] == 1
    assert report["gates"]["counts"]["gate_a"]["not_recorded"] == 1
    # Latest-per-video totals across the channel: only pid has analytics.
    assert report["performance"]["totals_from_latest_per_video"]["views"] == 250
    videos = {v["project_id"]: v for v in report["production"]["videos"]}
    assert videos[pid]["publish_classification"] == "simulated"
    assert videos[pid2]["cost_micros"] == 5000


def test_channel_report_period_filter_excludes_old_projects(env):
    factory, ch, niche = env["factory"], env["channel_id"], env["niche_id"]
    pid = seed_project(factory, ch, niche, topic="Timed")
    # seed_project stamps created_at with real now; bound a window around it.
    now = int(time.time() * 1000)
    inside = service.build_channel_report(
        factory, ch, period_start_ms=now - 60_000, period_end_ms=now + 60_000)
    assert inside["production"]["videos_produced"] == 1
    outside = service.build_channel_report(
        factory, ch, period_start_ms=now + 120_000, period_end_ms=now + 180_000)
    assert outside["production"]["videos_produced"] == 0
    assert outside["costs"]["total_micros"] == 0


def test_empty_channel_report_generates(env):
    factory = env["factory"]
    _ch2, _niche2 = seed_channel_niche(factory)
    report = service.build_channel_report(factory, _ch2)
    assert report["production"]["videos_produced"] == 0
    assert report["performance"]["available"] is False
    assert report["publish"]["classification"] == "not_published"


# ---------------------------------------------------------------------------
# Persistence seam (W2 contract)
# ---------------------------------------------------------------------------


def test_generate_persists_and_lists_and_fetches(env):
    pid = _rich_project(env)
    report = service.generate_video_report(
        env["factory"], pid, reports_dir=env["reports_dir"])
    assert (env["reports_dir"] / f"{report['report_id']}.json").exists()
    assert (env["reports_dir"] / f"{report['report_id']}.md").exists()

    metas = service.list_reports(env["factory"], channel_id=env["channel_id"])
    assert [m["report_id"] for m in metas] == [report["report_id"]]
    assert metas[0]["scope"] == "video"
    assert metas[0]["project_id"] == pid
    assert metas[0]["period_label"] == "lifetime"
    assert service.list_reports(env["factory"], scope="channel") == []
    assert service.list_reports(
        env["factory"], channel_id="other-channel") == []

    fetched = service.get_report(env["factory"], report["report_id"])
    assert fetched is not None
    assert fetched["report"]["costs"]["total_micros"] == 125000
    # Stored Markdown is exactly the rendering of the stored JSON.
    assert fetched["markdown"] == render_markdown(fetched["report"])
    assert service.get_report(env["factory"], "missing") is None
    assert service.get_report_markdown(env["factory"], "missing") is None


def test_build_does_not_persist(env):
    pid = _rich_project(env)
    service.build_video_report(env["factory"], pid)
    service.build_channel_report(env["factory"], env["channel_id"])
    assert service.list_reports(env["factory"]) == []


def test_generate_channel_report_persists_with_period_metadata(env):
    _rich_project(env)
    report = service.generate_channel_report(
        env["factory"], env["channel_id"],
        period_start_ms=_ms(-1000), period_end_ms=_ms(200_000),
        period_label="fixture_window", reports_dir=env["reports_dir"])
    metas = service.list_reports(env["factory"], scope="channel")
    assert len(metas) == 1
    assert metas[0]["period_label"] == "fixture_window"
    assert metas[0]["period_start_ms"] == _ms(-1000)
    assert report["period"]["label"] == "fixture_window"
