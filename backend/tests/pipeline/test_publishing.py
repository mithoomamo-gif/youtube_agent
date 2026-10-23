"""Publishing workflow tests (Section 34) — scripted FakeProvider, fake clock.

Run: pytest backend/tests/pipeline/test_publishing.py
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.pipeline.publishing import (  # noqa: E402
    AuthError,
    ChunkResult,
    FakeClock,
    IllegalTransitionError,
    InMemoryStore,
    InvalidPublishRequestError,
    ManifestError,
    PollOutcome,
    ProcessingPoller,
    PublishJob,
    PublishManifest,
    PublishState,
    PublishingWorkflow,
    QuotaExceededError,
    ReleaseMode,
    ServerError,
    SlotWatcher,
    TwoStepScheduler,
    UploadLimitExceededError,
    VideoMetadata,
    VideoStatus,
    VideoUpdate,
    WatcherAction,
    build_schedule_update,
    next_pacific_midnight,
    validate_publish_at,
)
from app.pipeline.publishing.uploader import ChunkedUploader  # noqa: E402

MIB = 1024 * 1024
DATA = b"x" * (2 * MIB + 5)


# --------------------------------------------------------------------------
# Fake provider
# --------------------------------------------------------------------------

class FakeProvider:
    """In-memory YouTube stand-in with scripting hooks.

    events: ordered log shared with RecordingStore for ordering asserts.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.events: list[tuple] = []
        self.sessions: dict[str, dict] = {}
        self.videos: dict[str, dict] = {}
        self._sc = 0
        self._vc = 0
        self.default_duration = 600.0
        # scripting hooks
        self.create_session_error: Exception | None = None
        self.chunk_hook = None  # fn(fake, uri, data, offset) -> ChunkResult|None
        self.status_fn = None  # fn(fake, video_id, poll_index) -> VideoStatus
        self._poll_index: dict[str, int] = {}

    # -- helpers for tests -------------------------------------------------
    def calls(self, method: str) -> list[tuple]:
        return [e for e in self.events if e[0] == method]

    def seed_video(self, title: str = "Test Title") -> str:
        self._vc += 1
        vid = f"vid-{self._vc}"
        self.videos[vid] = self._new_video(vid, title)
        return vid

    def _new_video(self, vid: str, title: str) -> dict:
        return {
            "title": title, "privacy": "private", "publish_at": None,
            "published_at": None, "duration": self.default_duration,
            "definition": "hd", "thumbnail": False, "captions": [],
        }

    # -- provider interface --------------------------------------------------
    def create_resumable_session(self, metadata, file_size):
        self.events.append(("create_resumable_session", metadata, file_size))
        if self.create_session_error is not None:
            raise self.create_session_error
        assert metadata.privacy_status == "private", "insert must be private"
        assert metadata.publish_at is None, "no publishAt at insert (two-step)"
        self._sc += 1
        uri = f"session://{self._sc}"
        self.sessions[uri] = {
            "metadata": metadata, "file_size": file_size,
            "confirmed": 0, "video_id": None,
        }
        return uri

    def _complete(self, session: dict) -> str:
        self._vc += 1
        vid = f"vid-{self._vc}"
        session["video_id"] = vid
        self.videos[vid] = self._new_video(vid, session["metadata"].title)
        return vid

    def upload_chunk(self, session_uri, data, offset):
        self.events.append(("upload_chunk", session_uri, offset, len(data)))
        session = self.sessions[session_uri]
        if self.chunk_hook is not None:
            hooked = self.chunk_hook(self, session_uri, data, offset)
            if hooked is not None:
                session["confirmed"] = hooked.confirmed_bytes
                if hooked.complete:
                    vid = self._complete(session)
                    return ChunkResult(201, session["file_size"], vid)
                return hooked
        if offset == session["confirmed"]:
            session["confirmed"] += len(data)
        if session["confirmed"] >= session["file_size"]:
            vid = self._complete(session)
            return ChunkResult(201, session["confirmed"], vid)
        return ChunkResult(308, session["confirmed"])

    def query_upload_status(self, session_uri):
        self.events.append(("query_upload_status", session_uri))
        session = self.sessions[session_uri]
        if session["video_id"]:
            return ChunkResult(201, session["file_size"], session["video_id"])
        return ChunkResult(308, session["confirmed"])

    def set_thumbnail(self, video_id, thumbnail_path):
        self.events.append(("set_thumbnail", video_id, thumbnail_path))
        self.videos[video_id]["thumbnail"] = True

    def insert_captions(self, video_id, srt_path, language):
        self.events.append(("insert_captions", video_id, srt_path, language))
        self.videos[video_id]["captions"].append(language)
        return "cap-1"

    def add_to_playlist(self, playlist_id, video_id):
        self.events.append(("add_to_playlist", playlist_id, video_id))
        return "pli-1"

    def update_video(self, video_id, update: VideoUpdate):
        self.events.append(("update_video", video_id, update))
        rec = self.videos[video_id]
        if update.privacy_status is not None:
            rec["privacy"] = update.privacy_status
        if update.clear_publish_at:
            rec["publish_at"] = None
        if update.publish_at is not None:
            rec["publish_at"] = update.publish_at
        if rec["privacy"] == "public" and rec["published_at"] is None:
            rec["published_at"] = self.clock.now()

    def get_video_status(self, video_id):
        self.events.append(("get_video_status", video_id))
        idx = self._poll_index.get(video_id, 0)
        self._poll_index[video_id] = idx + 1
        if self.status_fn is not None:
            return self.status_fn(self, video_id, idx)
        rec = self.videos[video_id]
        # Simulate YouTube flipping an armed slot public at its time.
        if (
            rec["publish_at"] is not None
            and rec["privacy"] == "private"
            and self.clock.now() >= rec["publish_at"]
        ):
            rec["privacy"] = "public"
            rec["published_at"] = rec["publish_at"]
        return VideoStatus(
            video_id=video_id,
            privacy_status=rec["privacy"],
            publish_at=rec["publish_at"],
            published_at=rec["published_at"],
            upload_status="processed",
            processing_status="succeeded",
            duration_seconds=rec["duration"],
            definition=rec["definition"],
            title=rec["title"],
            thumbnail_present=rec["thumbnail"],
            caption_languages=list(rec["captions"]),
        )


class RecordingStore(InMemoryStore):
    """Store that mirrors its writes into the provider's event log."""

    def __init__(self, events: list) -> None:
        super().__init__()
        self._events = events

    def save_video_id(self, idempotency_key, video_id):
        self._events.append(("save_video_id", video_id))
        super().save_video_id(idempotency_key, video_id)


class FakeRefresher:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls: list[str] = []

    def refresh(self, channel_id: str) -> bool:
        self.calls.append(channel_id)
        return self.result


# --------------------------------------------------------------------------
# Factories
# --------------------------------------------------------------------------

def make_manifest(clock: FakeClock, data: bytes = DATA, **over) -> PublishManifest:
    base = dict(
        project_id="proj-1",
        channel_id="chan-1",
        playlist_id="pl-1",
        master_path="/tmp/master.mp4",
        master_checksum="deadbeefcafe",
        master_size_bytes=len(data),
        duration_seconds=600.0,
        resolution_width=1920,
        resolution_height=1080,
        thumbnail_path="/tmp/thumb.jpg",
        caption_path="/tmp/caps.srt",
        caption_language="en",
        title="Test Title",
        description="Intro\n00:00 Start",
        tags=["facts"],
        category_id="22",
        qc_pass_token="qc-1",
        copyright_pass_token="cr-1",
        policy_pass_token="pol-1",
        licence_ledger_completeness=1.0,
    )
    base.update(over)
    return PublishManifest(**base).freeze()


def make_workflow(fake, store, clock, **kw) -> PublishingWorkflow:
    kw.setdefault("chunk_size", MIB)
    return PublishingWorkflow(fake, store, clock=clock, **kw)


def drive_to_checks_passed(fake, manifest) -> PublishJob:
    job = PublishJob(manifest=manifest)
    for st in (
        PublishState.PREFLIGHT_OK, PublishState.SESSION_OPEN,
        PublishState.UPLOADING, PublishState.UPLOADED,
        PublishState.ENRICHING, PublishState.PROCESSING,
        PublishState.CHECKS_PASSED,
    ):
        job.transition(st, "test drive")
    return job


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

def test_happy_path_full_walk_to_post_live_done():
    clock = FakeClock()
    fake = FakeProvider(clock)
    store = RecordingStore(fake.events)
    manifest = make_manifest(clock)
    wf = make_workflow(fake, store, clock)

    job = wf.run(manifest, DATA)

    assert job.state is PublishState.POST_LIVE_DONE
    assert job.video_id == "vid-1"
    assert job.is_live_confirmed
    assert job.published_at is not None
    assert job.verification is not None
    assert all(job.verification["checks"].values())
    visited = [r.dst for r in job.history]
    expected = [
        PublishState.PREFLIGHT_OK, PublishState.SESSION_OPEN,
        PublishState.UPLOADING, PublishState.UPLOADED,
        PublishState.ENRICHING, PublishState.PROCESSING,
        PublishState.CHECKS_PASSED, PublishState.RELEASING,
        PublishState.LIVE, PublishState.LIVE_CONFIRMED,
        PublishState.POST_LIVE_DONE,
    ]
    assert visited == expected
    # Enrichment happened in dependency order: thumbnail, captions, playlist.
    methods = [e[0] for e in fake.events]
    assert methods.index("set_thumbnail") < methods.index("insert_captions")
    assert methods.index("insert_captions") < methods.index("add_to_playlist")


# --------------------------------------------------------------------------
# Chunked transfer: server Range is authoritative
# --------------------------------------------------------------------------

def test_chunk_resume_honours_server_range_after_drop():
    clock = FakeClock()
    fake = FakeProvider(clock)
    store = InMemoryStore()
    manifest = make_manifest(clock)
    job = PublishJob(manifest=manifest)
    job.transition(PublishState.PREFLIGHT_OK, "test")

    dropped = {"done": False}

    def hook(fk, uri, data, offset):
        # First chunk: server only keeps half of what was sent (a drop),
        # and reports the truth via its Range/confirmed count.
        if not dropped["done"] and offset == 0:
            dropped["done"] = True
            return ChunkResult(308, confirmed_bytes=len(data) // 2)
        return None

    fake.chunk_hook = hook
    uploader = ChunkedUploader(fake, store, clock=clock, chunk_size=MIB)
    result = uploader.upload(job, manifest, DATA)

    assert result.video_id == "vid-1"
    offsets = [e[2] for e in fake.calls("upload_chunk")]
    # Second PUT starts at the SERVER-reported 512 KiB, not the client's
    # assumed 1 MiB.
    assert offsets[0] == 0
    assert offsets[1] == MIB // 2
    assert store.get_video_id(manifest.idempotency_key) == "vid-1"


def test_status_query_before_resend_on_5xx_and_retry_after_honoured():
    clock = FakeClock()
    fake = FakeProvider(clock)
    store = InMemoryStore()
    manifest = make_manifest(clock)
    job = PublishJob(manifest=manifest)
    job.transition(PublishState.PREFLIGHT_OK, "test")

    failed = {"done": False}

    def hook(fk, uri, data, offset):
        if not failed["done"]:
            failed["done"] = True
            raise ServerError("backend exploded", retry_after_seconds=7.0)
        return None

    fake.chunk_hook = hook
    uploader = ChunkedUploader(fake, store, clock=clock, chunk_size=MIB)
    result = uploader.upload(job, manifest, DATA)

    assert result.video_id == "vid-1"
    assert 7.0 in clock.sleeps  # Retry-After honoured
    methods = [e[0] for e in fake.events]
    first_chunk = methods.index("upload_chunk")
    query = methods.index("query_upload_status")
    second_chunk = methods.index("upload_chunk", first_chunk + 1)
    assert first_chunk < query < second_chunk  # query BEFORE any resend


def test_video_id_persisted_before_enrichment_calls():
    clock = FakeClock()
    fake = FakeProvider(clock)
    store = RecordingStore(fake.events)
    manifest = make_manifest(clock)
    wf = make_workflow(fake, store, clock)

    job = wf.run(manifest, DATA)

    assert job.state is PublishState.POST_LIVE_DONE
    methods = [e[0] for e in fake.events]
    saved = methods.index("save_video_id")
    assert saved < methods.index("set_thumbnail")
    assert saved < methods.index("insert_captions")
    assert saved < methods.index("add_to_playlist")
    # ... and the ledger holds it against the idempotency key.
    assert store.get_video_id(manifest.idempotency_key) == job.video_id


# --------------------------------------------------------------------------
# publishAt + public refusal
# --------------------------------------------------------------------------

def test_publish_at_with_public_construction_refused():
    from pydantic import ValidationError

    slot = FakeClock().now() + timedelta(hours=2)
    # Model construction refuses (Pydantic surfaces the refusal as a
    # ValidationError wrapping InvalidPublishRequestError).
    with pytest.raises(ValidationError):
        VideoMetadata(title="t", privacy_status="public", publish_at=slot)
    with pytest.raises(ValidationError):
        VideoUpdate(privacy_status="public", publish_at=slot)
    with pytest.raises(ValidationError):
        # publishAt without explicitly re-sending private is also refused.
        VideoUpdate(publish_at=slot)
    # The wrapper-level guard raises the typed error directly.
    with pytest.raises(InvalidPublishRequestError):
        validate_publish_at("unlisted", slot)
    with pytest.raises(InvalidPublishRequestError):
        validate_publish_at("public", slot)
    # The sanctioned construction works.
    ok = build_schedule_update(slot)
    assert ok.privacy_status == "private" and ok.publish_at == slot


# --------------------------------------------------------------------------
# Poller: backoff, timeout, P0D, rejections
# --------------------------------------------------------------------------

def _processing_status(fake, video_id, idx):
    return VideoStatus(
        video_id=video_id, privacy_status="private",
        upload_status="uploaded", processing_status="processing",
        duration_seconds=600.0, definition="sd", title="Test Title",
    )


def test_poller_backoff_sequence_and_6h_timeout_blocked_human():
    clock = FakeClock()
    fake = FakeProvider(clock)
    fake.status_fn = _processing_status
    manifest = make_manifest(clock)
    job = PublishJob(manifest=manifest)
    job.video_id = fake.seed_video()
    job.transition(PublishState.PREFLIGHT_OK, "t")
    for st in (PublishState.SESSION_OPEN, PublishState.UPLOADING,
               PublishState.UPLOADED, PublishState.ENRICHING,
               PublishState.PROCESSING):
        job.transition(st, "t")

    poller = ProcessingPoller(fake, clock=clock)  # default 6h timeout
    result = poller.poll(job, manifest)

    assert result.outcome is PollOutcome.TIMEOUT
    assert job.state is PublishState.BLOCKED_HUMAN
    assert clock.sleeps[:5] == [30.0, 60.0, 120.0, 300.0, 600.0]
    assert all(s == 600.0 for s in clock.sleeps[5:])
    assert result.elapsed_seconds >= 6 * 3600


def test_p0d_dead_upload_detection():
    clock = FakeClock()
    fake = FakeProvider(clock)

    def p0d(fk, video_id, idx):
        return VideoStatus(
            video_id=video_id, privacy_status="private",
            upload_status="uploaded", processing_status="processing",
            duration_seconds=0.0, definition="sd", title="Test Title",
        )

    fake.status_fn = p0d
    manifest = make_manifest(clock)
    job = PublishJob(manifest=manifest)
    job.video_id = fake.seed_video()
    for st in (PublishState.PREFLIGHT_OK, PublishState.SESSION_OPEN,
               PublishState.UPLOADING, PublishState.UPLOADED,
               PublishState.ENRICHING, PublishState.PROCESSING):
        job.transition(st, "t")

    result = ProcessingPoller(fake, clock=clock).poll(job, manifest)

    assert result.outcome is PollOutcome.DEAD_UPLOAD
    assert "P0D" in result.detail
    assert result.elapsed_seconds > 3600


def test_duplicate_rejection_is_terminal():
    clock = FakeClock()
    fake = FakeProvider(clock)
    fake.status_fn = lambda fk, vid, i: VideoStatus(
        video_id=vid, rejection_reason="duplicate",
        upload_status="rejected", processing_status="succeeded",
        duration_seconds=600.0, definition="hd", title="Test Title",
    )
    manifest = make_manifest(clock)
    job = PublishJob(manifest=manifest)
    job.video_id = fake.seed_video()
    for st in (PublishState.PREFLIGHT_OK, PublishState.SESSION_OPEN,
               PublishState.UPLOADING, PublishState.UPLOADED,
               PublishState.ENRICHING, PublishState.PROCESSING):
        job.transition(st, "t")

    result = ProcessingPoller(fake, clock=clock).poll(job, manifest)

    assert result.outcome is PollOutcome.FAILED_TERMINAL
    assert job.state is PublishState.FAILED_TERMINAL
    assert job.channel_demoted is False


def test_claim_rejection_blocks_human_and_demotes_channel():
    clock = FakeClock()
    fake = FakeProvider(clock)
    fake.status_fn = lambda fk, vid, i: VideoStatus(
        video_id=vid, rejection_reason="claim",
        upload_status="rejected", processing_status="succeeded",
        duration_seconds=600.0, definition="hd", title="Test Title",
    )
    manifest = make_manifest(clock)
    job = PublishJob(manifest=manifest)
    job.video_id = fake.seed_video()
    for st in (PublishState.PREFLIGHT_OK, PublishState.SESSION_OPEN,
               PublishState.UPLOADING, PublishState.UPLOADED,
               PublishState.ENRICHING, PublishState.PROCESSING):
        job.transition(st, "t")

    result = ProcessingPoller(fake, clock=clock).poll(job, manifest)

    assert result.outcome is PollOutcome.BLOCKED_HUMAN
    assert job.state is PublishState.BLOCKED_HUMAN
    assert job.channel_demoted is True


# --------------------------------------------------------------------------
# Two-step scheduling + watcher
# --------------------------------------------------------------------------

def test_two_step_scheduling_picks_fallback_when_slot_too_close():
    clock = FakeClock()
    fake = FakeProvider(clock)
    store = InMemoryStore()
    now = clock.now()
    target = now + timedelta(minutes=5)     # < 30 min minimum lead
    fallback = now + timedelta(hours=2)
    manifest = make_manifest(
        clock, release_mode=ReleaseMode.SCHEDULED,
        target_slot=target, fallback_slots=[fallback],
    )
    wf = make_workflow(fake, store, clock)

    job = wf.run(manifest, DATA)

    assert job.state is PublishState.SCHEDULED
    assert job.armed_slot == fallback
    # The arming update re-sent privacy=private explicitly with publishAt.
    updates = [e[2] for e in fake.calls("update_video")]
    assert any(
        u.publish_at == fallback and u.privacy_status == "private"
        for u in updates
    )
    # Nothing was public before the slot, and no publishAt existed at insert.
    insert_meta = fake.calls("create_resumable_session")[0][1]
    assert insert_meta.publish_at is None

    # At the slot, release verifies and completes.
    clock.advance((fallback - clock.now()).total_seconds())
    wf.release(job, manifest)
    assert job.state is PublishState.POST_LIVE_DONE
    assert job.published_at == fallback


def test_watcher_cancels_publish_at_when_checks_incomplete_near_slot():
    clock = FakeClock()
    fake = FakeProvider(clock)
    now = clock.now()
    target = now + timedelta(minutes=20)
    fallback = now + timedelta(hours=3)
    manifest = make_manifest(
        clock, release_mode=ReleaseMode.SCHEDULED,
        target_slot=target, fallback_slots=[fallback],
    )
    scheduler = TwoStepScheduler(fake, min_lead_seconds=60.0)
    watcher = SlotWatcher(fake, scheduler, cancel_window_seconds=1800.0)

    job = drive_to_checks_passed(fake, manifest)
    job.video_id = fake.seed_video()
    # Rewind the drive: scheduler needs CHECKS_PASSED, which we have.
    result = scheduler.schedule(job, manifest, now)
    assert result.slot == target and job.state is PublishState.SCHEDULED

    action = watcher.check(
        job, manifest, now + timedelta(minutes=10), checks_complete=False
    )

    assert action is WatcherAction.CANCELLED_AND_REBOOKED
    updates = [e[2] for e in fake.calls("update_video")]
    cancel_idx = next(i for i, u in enumerate(updates) if u.clear_publish_at)
    rebook_idx = next(
        i for i, u in enumerate(updates) if u.publish_at == fallback
    )
    assert cancel_idx < rebook_idx  # cancel happened BEFORE re-booking
    assert job.state is PublishState.SCHEDULED
    assert job.armed_slot == fallback


def test_watcher_cancel_without_fallback_leaves_plain_private():
    clock = FakeClock()
    fake = FakeProvider(clock)
    now = clock.now()
    target = now + timedelta(minutes=20)
    manifest = make_manifest(
        clock, release_mode=ReleaseMode.SCHEDULED, target_slot=target,
    )
    scheduler = TwoStepScheduler(fake, min_lead_seconds=60.0)
    watcher = SlotWatcher(fake, scheduler, cancel_window_seconds=1800.0)
    job = drive_to_checks_passed(fake, manifest)
    job.video_id = fake.seed_video()
    scheduler.schedule(job, manifest, now)

    action = watcher.check(
        job, manifest, now + timedelta(minutes=15), checks_complete=False
    )

    assert action is WatcherAction.CANCELLED_NO_FALLBACK
    assert job.state is PublishState.CHECKS_PASSED
    assert job.armed_slot is None
    assert fake.videos[job.video_id]["publish_at"] is None
    assert fake.videos[job.video_id]["privacy"] == "private"


# --------------------------------------------------------------------------
# Failure matrix via the workflow
# --------------------------------------------------------------------------

def test_quota_exceeded_parks_job_until_pacific_midnight():
    clock = FakeClock()
    fake = FakeProvider(clock)
    fake.create_session_error = QuotaExceededError("daily quota gone")
    store = InMemoryStore()
    manifest = make_manifest(clock)
    wf = make_workflow(fake, store, clock)

    job = wf.run(manifest, DATA)

    assert job.parked_until == next_pacific_midnight(clock.now())
    assert job.park_reason and "quotaExceeded" in job.park_reason
    assert job.state is PublishState.PREFLIGHT_OK  # parked, not spun/failed
    assert len(fake.calls("create_resumable_session")) == 1  # no retry spin


def test_upload_limit_exceeded_is_terminal_for_the_day():
    clock = FakeClock()
    fake = FakeProvider(clock)
    fake.create_session_error = UploadLimitExceededError("channel cap")
    manifest = make_manifest(clock)
    wf = make_workflow(fake, InMemoryStore(), clock)

    job = wf.run(manifest, DATA)

    assert job.state is PublishState.FAILED_TERMINAL
    assert "uploadLimitExceeded" in (job.failure_reason or "")


def test_auth_failure_refresh_once_then_blocked_human():
    clock = FakeClock()
    fake = FakeProvider(clock)
    fake.create_session_error = AuthError("token revoked")
    refresher = FakeRefresher(result=False)
    manifest = make_manifest(clock)
    wf = PublishingWorkflow(
        fake, InMemoryStore(), clock=clock,
        token_refresher=refresher, chunk_size=MIB,
    )

    job = wf.run(manifest, DATA)

    assert refresher.calls == ["chan-1"]  # exactly one refresh attempt
    assert job.state is PublishState.BLOCKED_HUMAN
    assert "chan-1" in (job.failure_reason or "")


def test_auth_refresh_success_retries_step():
    clock = FakeClock()
    fake = FakeProvider(clock)
    store = InMemoryStore()
    manifest = make_manifest(clock)

    attempts = {"n": 0}
    real_create = fake.create_resumable_session

    def flaky(metadata, file_size):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise AuthError("expired")
        return real_create(metadata, file_size)

    fake.create_resumable_session = flaky
    wf = PublishingWorkflow(
        fake, store, clock=clock,
        token_refresher=FakeRefresher(result=True), chunk_size=MIB,
    )

    job = wf.run(manifest, DATA)

    assert job.state is PublishState.POST_LIVE_DONE
    assert attempts["n"] == 2


# --------------------------------------------------------------------------
# Manifest + state machine units
# --------------------------------------------------------------------------

def test_manifest_freeze_binds_hash_and_tokens():
    clock = FakeClock()
    manifest = make_manifest(clock)
    assert manifest.is_frozen and manifest.manifest_hash
    assert manifest.manifest_hash == manifest.compute_hash()
    assert manifest.idempotency_key == PublishManifest(
        **{**manifest.model_dump(), "manifest_hash": None, "is_frozen": False}
    ).idempotency_key

    with pytest.raises(ManifestError):
        PublishManifest(**{
            **manifest.model_dump(),
            "manifest_hash": None, "is_frozen": False,
            "policy_pass_token": None,
        }).freeze()
    with pytest.raises(ManifestError):
        PublishManifest(**{
            **manifest.model_dump(),
            "manifest_hash": None, "is_frozen": False,
            "licence_ledger_completeness": 0.9,
        }).freeze()


def test_manifest_derive_new_version_invalidates_approvals():
    clock = FakeClock()
    manifest = make_manifest(clock)
    v2 = manifest.derive_new_version(title="A Different Title")

    assert v2.manifest_version == 2
    assert v2.is_frozen is False and v2.manifest_hash is None
    assert v2.qc_pass_token is None
    assert v2.copyright_pass_token is None
    assert v2.policy_pass_token is None
    assert v2.idempotency_key != manifest.idempotency_key  # title is bound in
    with pytest.raises(Exception):  # frozen model: direct edit impossible
        manifest.title = "sneaky"  # type: ignore[misc]


def test_illegal_transition_rejected():
    clock = FakeClock()
    manifest = make_manifest(clock)
    job = PublishJob(manifest=manifest)
    with pytest.raises(IllegalTransitionError):
        job.transition(PublishState.LIVE, "skip everything")
    with pytest.raises(IllegalTransitionError):
        job.transition(PublishState.POST_LIVE_DONE, "and done")
    job.transition(PublishState.PREFLIGHT_OK, "ok")
    with pytest.raises(IllegalTransitionError):
        job.transition(PublishState.LIVE_CONFIRMED, "still illegal")


def test_unfrozen_manifest_blocked_at_g0():
    clock = FakeClock()
    fake = FakeProvider(clock)
    manifest = PublishManifest(**{
        **make_manifest(clock).model_dump(),
        "manifest_hash": None, "is_frozen": False,
        "qc_pass_token": None,
    })
    wf = make_workflow(fake, InMemoryStore(), clock)

    job = wf.run(manifest, DATA)

    assert job.state is PublishState.BLOCKED_HUMAN
    assert fake.events == []  # no byte moved, no API call made
