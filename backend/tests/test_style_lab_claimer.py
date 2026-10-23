"""Style Lab claimer tests (SL-002 — the missing background claimer).

Covers the §7 job contract in ``docs/STYLE_LAB.md`` as implemented by
``app.style_analysis.claimer.CompetitorAnalysisClaimer`` and hosted by
``app.execution.service.ExecutionService``:

1. End-to-end: a competitor video attached through the real API and
   queued via ``POST .../competitor-videos/{id}/analysis`` is claimed
   by the claimer, analysed by the real engine entry point (heavy
   stages faked exactly like ``tests/stylelab/test_engine.py``; the
   claimer's ffprobe pre-flight is REAL), lands in ``'analysed'``
   with ``analysis.json`` + ``breakdown.md`` + an ``index.json``
   entry in the §6 layout.
2. Failure: a corrupt attached file flips to ``'analysis_failed'``
   with a durable, non-empty recorded reason
   (``<data_dir>/stylelab/<video id>/analysis_failure.json``), and
   the claimer survives to process a subsequent good row.
3. Scope: queued rows from two channels are both processed in one
   pass, and each video's outputs / index entry carry only its own
   owning channel.
4. STOP ALL / pause / shutdown: with the persisted ``stop_all`` (or
   ``pause_all``) flag active the claimer claims nothing; the
   ExecutionService-hosted loop claims nothing under STOP ALL and,
   with flags clear, drives a queued row to ``'analysed'`` and is
   fully stopped by ``service.stop()``.
5. The claim itself is conditional: a second claim of an already
   claimed row is refused (no double-claim).

No network, no API keys: media fixtures are tiny real files generated
with ffmpeg lavfi (the repo's existing fixture pattern).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.style_analysis import engine, schema as sl_schema
from tests.execution_fixtures import make_db

# ---------------------------------------------------------------------------
# Engine stage fakes (same pattern as tests/stylelab/test_engine.py —
# vision/speech/etc. are mocked; the orchestration + persistence are real)
# ---------------------------------------------------------------------------


def _fake_probe(_path):
    return {
        "path": str(_path), "exists": True, "size_bytes": 1234,
        "duration_s": 60.0, "width": 1280, "height": 720,
        "video_codec": "h264", "fps": 30.0,
        "has_audio": True, "has_video": True,
    }


def _fake_scenes(_path, _duration):
    segs = [
        sl_schema.SceneSegment(index=0, start_s=0.0, end_s=30.0),
        sl_schema.SceneSegment(index=1, start_s=30.0, end_s=60.0),
    ]
    return sl_schema.SceneAnalysis.from_segments(segs, total_duration_s=60.0)


def _fake_visual(_path, _scenes):
    labels = [
        sl_schema.SceneVisualLabel(
            scene_index=0, label="stock_like", confidence=0.8),
        sl_schema.SceneVisualLabel(
            scene_index=1, label="uncertain", confidence=0.3),
    ]
    return sl_schema.VisualMix.from_scene_labels(
        labels, list(_scenes.scenes), confidence=0.55)


def _fake_speech(_path, _scenes, _work):
    return sl_schema.SpeechAnalysis(
        transcript_languages=["ur"], language_confidence=0.9, wpm=150.0)


def _fake_dressing(_path, _scenes, _speech):
    return sl_schema.DressingAnalysis(
        music_presence=sl_schema.PresenceAssessment(
            label="present", confidence=0.7))


def _fake_thumbnail(_path):
    return sl_schema.ThumbnailAnalysis(
        text_word_count=3, layout_class="mixed", contrast_class="high",
        confidence=0.6)


@pytest.fixture()
def patched_stages(monkeypatch):
    monkeypatch.setattr(engine, "probe_video", _fake_probe)
    monkeypatch.setattr(engine, "_stage_scenes", _fake_scenes)
    monkeypatch.setattr(engine, "_stage_visual", _fake_visual)
    monkeypatch.setattr(engine, "_stage_speech", _fake_speech)
    monkeypatch.setattr(engine, "_stage_dressing", _fake_dressing)
    monkeypatch.setattr(engine, "_stage_thumbnail", _fake_thumbnail)
    return monkeypatch


# ---------------------------------------------------------------------------
# Media fixtures (tiny REAL files via ffmpeg lavfi, as in the e2e test)
# ---------------------------------------------------------------------------


def _ffmpeg(*args: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not available for media fixtures")
    proc = subprocess.run([ffmpeg, "-y", *args],
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-500:]


def _make_clip(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-f", "lavfi", "-i",
            "testsrc=duration=1:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", str(path))
    return path


def _make_thumb(video: Path, path: Path) -> Path:
    _ffmpeg("-i", str(video), "-frames:v", "1", str(path))
    return path


# ---------------------------------------------------------------------------
# Isolated-DB environment (store-level fixtures)
# ---------------------------------------------------------------------------


def _seed_channel(factory, label: str) -> str:
    channel_id = str(uuid.uuid4())
    ts = int(time.time() * 1000)
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language,"
            " status, created_at, updated_at) VALUES (?, ?, ?, 'en-US',"
            " 'active', ?, ?)",
            (channel_id, f"UC-claimer-{label}-{uuid.uuid4().hex[:8]}",
             f"Owner {label}", ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return channel_id


@pytest.fixture()
def env(tmp_path):
    from app.style_lab.parsing import parse_competitor_input
    from app.style_lab.store import StyleLabStore
    from app.style_analysis.claimer import CompetitorAnalysisClaimer

    _db_path, factory = make_db(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = StyleLabStore(factory)
    claimer = CompetitorAnalysisClaimer(factory, data_dir=data_dir)

    def add_video(channel_id: str, handle: str, *, good: bool = True,
                  with_thumbnail: bool = False) -> dict:
        competitor = store.create_competitor(
            channel_id, parse_competitor_input(f"@{handle}"))
        rel_dir = Path("style_lab") / channel_id / competitor["id"]
        video_rel = rel_dir / ("clip.mp4" if good else "corrupt.mp4")
        video_abs = data_dir / video_rel
        video_abs.parent.mkdir(parents=True, exist_ok=True)
        if good:
            _make_clip(video_abs)
        else:
            video_abs.write_bytes(b"this is not a video file at all" * 8)
        thumb_rel = None
        if with_thumbnail and good:
            thumb_rel = rel_dir / "thumb.jpg"
            _make_thumb(video_abs, data_dir / thumb_rel)
        return store.add_video(
            competitor["id"],
            {
                "source": "attached_file",
                "title": f"Video for {handle}",
                "storage_path": video_rel.as_posix(),
                "thumbnail_storage_path": (
                    thumb_rel.as_posix() if thumb_rel else None),
                "file_sha256": hashlib.sha256(
                    video_abs.read_bytes()).hexdigest(),
                "file_size_bytes": video_abs.stat().st_size,
                "mime_type": "video/mp4",
                "duration_s": 1.0, "width": 320, "height": 240,
            },
            channel_id=channel_id,
        )

    return {
        "factory": factory, "store": store, "claimer": claimer,
        "data_dir": data_dir, "add_video": add_video,
        "channel_a": _seed_channel(factory, "A"),
        "channel_b": _seed_channel(factory, "B"),
    }


def _status(store, video_id: str) -> str:
    row = store.get_video(video_id)
    assert row is not None
    return row["analysis_status"]


# ---------------------------------------------------------------------------
# 1. End-to-end through the real API
# ---------------------------------------------------------------------------


def test_api_queued_analysis_is_claimed_and_completed(
    client: TestClient, patched_stages, tmp_path: Path
):
    from app import db as db_module
    from app.config import get_settings
    from app.style_analysis.claimer import CompetitorAnalysisClaimer

    suffix = uuid.uuid4().hex[:10]
    chan = f"sl-claim-{suffix}"
    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO channels (id, youtube_channel_id, title,"
            " language, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'en', 'active', ?, ?)",
            (chan, f"UCclaim{suffix}xxxx", "Claimer Owner", now, now),
        )
    finally:
        conn.close()

    comp_resp = client.post(
        f"/api/v1/channels/{chan}/competitors",
        json={"input": f"https://www.youtube.com/@ClaimRival{suffix[:6]}"},
    )
    assert comp_resp.status_code == 201, comp_resp.text
    comp_id = comp_resp.json()["id"]

    clip = _make_clip(tmp_path / "api-clip.mp4")
    thumb = _make_thumb(clip, tmp_path / "api-thumb.jpg")
    attach_resp = client.post(
        f"/api/v1/channels/{chan}/competitors/{comp_id}/videos/attach",
        files={
            "video": ("api-clip.mp4", clip.read_bytes(), "video/mp4"),
            "thumbnail": ("api-thumb.jpg", thumb.read_bytes(), "image/jpeg"),
        },
        data={"title": "Claimer E2E Rival Video"},
    )
    assert attach_resp.status_code == 201, attach_resp.text
    video_id = attach_resp.json()["id"]
    assert attach_resp.json()["analysis_status"] == "not_queued"

    queue_resp = client.post(
        f"/api/v1/channels/{chan}/competitor-videos/{video_id}/analysis")
    assert queue_resp.status_code == 200, queue_resp.text
    assert queue_resp.json()["analysis_status"] == "queued"

    data_dir = Path(get_settings().data_dir)
    claimer = CompetitorAnalysisClaimer(
        lambda: db_module.get_connection(),
        data_dir=data_dir, max_per_poll=50,
    )
    reports = claimer.poll_once()
    mine = [r for r in reports if r["video_id"] == video_id]
    assert len(mine) == 1 and mine[0]["status"] == "analysed", reports

    videos = client.get(
        f"/api/v1/channels/{chan}/competitors/{comp_id}/videos"
    ).json()["items"]
    by_id = {v["id"]: v for v in videos}
    assert by_id[video_id]["analysis_status"] == "analysed"

    # §6 analysis layout: per-video analysis.json + breakdown.md, and an
    # index.json entry keyed by the video id carrying the owner channel.
    video_dir = data_dir / "stylelab" / video_id
    assert (video_dir / "analysis.json").is_file()
    assert (video_dir / "breakdown.md").is_file()
    assert not (video_dir / "analysis_failure.json").exists()
    saved = json.loads((video_dir / "analysis.json").read_text("utf-8"))
    assert saved["competitor_video_id"] == video_id
    assert saved["channel_id"] == chan
    index = json.loads((data_dir / "stylelab" / "index.json").read_text("utf-8"))
    entries = {e["competitor_video_id"]: e for e in index["entries"]}
    assert entries[video_id]["channel_id"] == chan


# ---------------------------------------------------------------------------
# 2. Failure: corrupt file -> analysis_failed + recorded reason; survives
# ---------------------------------------------------------------------------


def test_corrupt_file_fails_with_recorded_reason_and_claimer_survives(
    env, patched_stages
):
    store, claimer = env["store"], env["claimer"]
    bad = env["add_video"](env["channel_a"], "CorruptRival", good=False)
    store.set_analysis_status(bad["id"], "queued", channel_id=env["channel_a"])
    assert _status(store, bad["id"]) == "queued"

    reports = claimer.poll_once()
    assert len(reports) == 1
    report = reports[0]
    assert report["video_id"] == bad["id"]
    assert report["status"] == "analysis_failed"
    assert report["reason"]  # non-empty machine-readable reason
    assert _status(store, bad["id"]) == "analysis_failed"

    # The recorded reason is durable, in the §6 analysis layout.
    failure_path = (
        env["data_dir"] / "stylelab" / bad["id"] / "analysis_failure.json")
    assert failure_path.is_file()
    recorded = json.loads(failure_path.read_text("utf-8"))
    assert recorded["competitor_video_id"] == bad["id"]
    assert recorded["channel_id"] == env["channel_a"]
    assert recorded["status"] == "analysis_failed"
    assert recorded["reason"] == report["reason"]
    assert recorded["detail"]
    # No fake success artefacts for the corrupt video.
    assert not (
        env["data_dir"] / "stylelab" / bad["id"] / "analysis.json").exists()

    # The claimer survived the failure: a subsequent good row processes.
    good = env["add_video"](
        env["channel_a"], "GoodRival", good=True, with_thumbnail=True)
    store.set_analysis_status(good["id"], "queued", channel_id=env["channel_a"])
    reports = claimer.poll_once()
    assert len(reports) == 1
    assert reports[0]["video_id"] == good["id"]
    assert reports[0]["status"] == "analysed"
    assert _status(store, good["id"]) == "analysed"
    assert (
        env["data_dir"] / "stylelab" / good["id"] / "analysis.json").is_file()


# ---------------------------------------------------------------------------
# 3. Scope: two channels' queues, outputs stay in their own scope
# ---------------------------------------------------------------------------


def test_two_channels_outputs_stay_in_own_scope(env, patched_stages):
    store, claimer = env["store"], env["claimer"]
    video_a = env["add_video"](env["channel_a"], "ScopeRivalA", good=True)
    video_b = env["add_video"](env["channel_b"], "ScopeRivalB", good=True)
    store.set_analysis_status(
        video_a["id"], "queued", channel_id=env["channel_a"])
    store.set_analysis_status(
        video_b["id"], "queued", channel_id=env["channel_b"])

    reports = claimer.poll_once()
    assert {r["video_id"] for r in reports} == {video_a["id"], video_b["id"]}
    assert all(r["status"] == "analysed" for r in reports)
    assert _status(store, video_a["id"]) == "analysed"
    assert _status(store, video_b["id"]) == "analysed"

    base = env["data_dir"] / "stylelab"
    for video, channel in (
        (video_a, env["channel_a"]), (video_b, env["channel_b"])
    ):
        saved = json.loads(
            (base / video["id"] / "analysis.json").read_text("utf-8"))
        assert saved["competitor_video_id"] == video["id"]
        assert saved["channel_id"] == channel
        assert (base / video["id"] / "breakdown.md").is_file()

    index = json.loads((base / "index.json").read_text("utf-8"))
    entries = {e["competitor_video_id"]: e for e in index["entries"]}
    assert entries[video_a["id"]]["channel_id"] == env["channel_a"]
    assert entries[video_b["id"]]["channel_id"] == env["channel_b"]
    # The file store itself loads each video's own analysis back.
    from app.style_analysis.storage import StyleAnalysisStore as FileStore

    file_store = FileStore(base)
    assert file_store.load(video_a["id"]).channel_id == env["channel_a"]
    assert file_store.load(video_b["id"]).channel_id == env["channel_b"]


# ---------------------------------------------------------------------------
# 4. STOP ALL / pause block claiming; service hosting + shutdown
# ---------------------------------------------------------------------------


def test_stop_all_and_pause_block_claiming(env, patched_stages):
    from app.execution.flags import set_flag

    store, claimer, factory = env["store"], env["claimer"], env["factory"]
    video = env["add_video"](env["channel_a"], "FlagRival", good=True)
    store.set_analysis_status(video["id"], "queued", channel_id=env["channel_a"])

    set_flag(factory, "stop_all", True)
    try:
        assert claimer.poll_once() == []
        assert _status(store, video["id"]) == "queued"
        assert claimer.stop_requested is True
    finally:
        set_flag(factory, "stop_all", False)

    set_flag(factory, "pause_all", True)
    try:
        assert claimer.poll_once() == []
        assert _status(store, video["id"]) == "queued"
    finally:
        set_flag(factory, "pause_all", False)

    # Flags clear: the same row is claimable again (nothing was lost).
    reports = claimer.poll_once()
    assert len(reports) == 1 and reports[0]["status"] == "analysed"
    assert _status(store, video["id"]) == "analysed"


def test_execution_service_hosts_claimer_and_shutdown_stops_it(
    env, patched_stages
):
    from app.execution.flags import set_flag
    from app.execution.service import ExecutionService

    store, factory = env["store"], env["factory"]
    video = env["add_video"](env["channel_a"], "HostedRival", good=True)
    store.set_analysis_status(video["id"], "queued", channel_id=env["channel_a"])

    service = ExecutionService(
        factory,
        scheduler_initial_delay_s=3600.0,  # keep scheduler ticks out
        stylelab_initial_delay_s=0.05,
        stylelab_interval_s=0.05,
        stylelab_data_dir=env["data_dir"],
    )
    assert service.stylelab_claimer is not None

    async def _phase_stop_all() -> None:
        set_flag(factory, "stop_all", True)
        try:
            await service.start()
            await asyncio.sleep(0.6)
            # STOP ALL: the hosted claimer claimed nothing.
            assert _status(store, video["id"]) == "queued"
        finally:
            await service.stop()
            set_flag(factory, "stop_all", False)

    async def _phase_runs_and_stops() -> None:
        await service.start()
        try:
            assert service._claimer_task is not None
            assert not service._claimer_task.done()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if _status(store, video["id"]) == "analysed":
                    break
                await asyncio.sleep(0.05)
        finally:
            await service.stop()

    asyncio.run(_phase_stop_all())
    asyncio.run(_phase_runs_and_stops())

    assert _status(store, video["id"]) == "analysed"
    assert (
        env["data_dir"] / "stylelab" / video["id"] / "analysis.json"
    ).is_file()
    # Shutdown really stopped the claimer loop.
    assert service._claimer_task is None
    assert service.status()["running"] is False


# ---------------------------------------------------------------------------
# 5. Conditional claim: no double-claim
# ---------------------------------------------------------------------------


def test_claim_is_conditional_no_double_claim(env, patched_stages):
    store, claimer = env["store"], env["claimer"]
    video = env["add_video"](env["channel_a"], "ClaimRival", good=True)
    store.set_analysis_status(video["id"], "queued", channel_id=env["channel_a"])

    queued = claimer.list_queued()
    assert [q["id"] for q in queued] == [video["id"]]
    assert queued[0]["channel_id"] == env["channel_a"]

    claimed = claimer.claim(queued[0])
    assert claimed is not None
    assert claimed["analysis_status"] == "analysing"

    # A second claim of the same row (another poller / a stale scan)
    # is refused by the status guard and leaves the row untouched.
    assert claimer.claim(queued[0]) is None
    assert _status(store, video["id"]) == "analysing"
    # And it no longer appears in the queued scan.
    assert claimer.list_queued() == []
