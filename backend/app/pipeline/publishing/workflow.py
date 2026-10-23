"""PublishingWorkflow — orchestrates manifest → LIVE_CONFIRMED.

Phases: G0/G1 preflight → resumable upload → enrichment (while YouTube
processes) → processing poll (G3) → two-step scheduling OR immediate
release (G4) → post-live verification (G5) → POST_LIVE_DONE.

Failure matrix (Section 34) is mapped here:
- 401/token revoked → refresh ONCE → BLOCKED_HUMAN re-auth (that
  channel only; other channels' jobs are untouched).
- 403 quotaExceeded → park until Pacific-midnight reset, no retry spin.
- 403 uploadLimitExceeded → FAILED_TERMINAL for the day.
- rejectionReason duplicate → FAILED_TERMINAL (in the poller).
- rejectionReason claim/copyright → BLOCKED_HUMAN + channel demotion
  flag (handled in the poller).
"""
from __future__ import annotations

from typing import Callable, Protocol, TypeVar

from .clock import Clock, SystemClock
from .errors import (
    AuthError,
    ComplianceGateError,
    QuotaExceededError,
    UploadLimitExceededError,
)
from .manifest import PublishManifest, ReleaseMode
from .poller import PollOutcome, ProcessingPoller
from .provider import PRIVACY_PUBLIC, YouTubeProvider, build_release_update
from .scheduler import TwoStepScheduler, next_pacific_midnight
from .state_machine import PublishJob, PublishState
from .store import UploadStore
from .uploader import ChunkedUploader

T = TypeVar("T")


class TokenRefresher(Protocol):
    """Central Token Manager surface: refresh one channel's token."""

    def refresh(self, channel_id: str) -> bool: ...


class _WorkflowStopped(Exception):
    """Internal: a mapped failure already placed the job in its state."""


class PublishingWorkflow:
    def __init__(
        self,
        provider: YouTubeProvider,
        store: UploadStore,
        *,
        clock: Clock | None = None,
        token_refresher: TokenRefresher | None = None,
        chunk_size: int | None = None,
        require_hd: bool = True,
        processing_timeout_seconds: float | None = None,
        min_lead_seconds: float | None = None,
        max_dead_upload_reuploads: int = 1,
        compliance_checker=None,
    ) -> None:
        self._provider = provider
        self._store = store
        # Compliance Gate (Round 6, Gate C): None = no gate configured
        # (legacy behaviour, used by pure unit tests).  Production
        # paths (publish_stage, publish finaliser) always pass a
        # DB-backed checker; while it does not report overall
        # ``passed``, uploads are forced private and any public
        # release is refused with ComplianceGateError.
        self._compliance_checker = compliance_checker
        self._clock = clock or SystemClock()
        self._refresher = token_refresher
        self._uploader = ChunkedUploader(
            provider, store, clock=self._clock,
            **({"chunk_size": chunk_size} if chunk_size else {}),
        )
        self._poller = ProcessingPoller(
            provider, clock=self._clock, require_hd=require_hd,
            **(
                {"timeout_seconds": processing_timeout_seconds}
                if processing_timeout_seconds
                else {}
            ),
        )
        self._scheduler = TwoStepScheduler(
            provider,
            **({"min_lead_seconds": min_lead_seconds} if min_lead_seconds else {}),
        )
        self._max_dead_reuploads = max_dead_upload_reuploads

    # -- compliance gate (Gate C, Round 6) --------------------------------------

    def compliance_decision(self, requested_visibility: str = "public"):
        """Visibility decision for this workflow, or None if ungated."""
        if self._compliance_checker is None:
            return None
        from app.compliance.gate import evaluate_visibility, resolve_checker

        overall = resolve_checker(self._compliance_checker)
        return evaluate_visibility(requested_visibility, overall)

    def _apply_compliance_hold(self, job: PublishJob) -> ComplianceGateError:
        """Record the forced-private hold on ``job``; return the typed error."""
        decision = self.compliance_decision("public")
        assert decision is not None
        job.compliance_forced_private = True
        job.compliance_block = {
            "code": ComplianceGateError.code,
            "reason": ComplianceGateError.reason,
            "overall": decision["overall"],
            "requested_visibility": decision["requested_visibility"],
            "effective_visibility": decision["effective_visibility"],
            "forced_private": True,
            "message": decision["message"],
        }
        return ComplianceGateError(
            overall=decision["overall"], requested_visibility="public"
        )

    # -- error mapping --------------------------------------------------------

    def _call(self, job: PublishJob, fn: Callable[[], T]) -> T:
        """Run a provider step with the failure matrix applied."""
        try:
            return fn()
        except AuthError:
            # Refresh once, retry the current step only.
            refreshed = False
            if self._refresher is not None:
                refreshed = self._refresher.refresh(job.manifest.channel_id)
            if refreshed:
                try:
                    return fn()
                except AuthError:
                    pass
            job.transition(
                PublishState.BLOCKED_HUMAN,
                f"auth failed for channel {job.manifest.channel_id}; "
                "re-auth required (this channel only)",
            )
            raise _WorkflowStopped
        except QuotaExceededError:
            until = next_pacific_midnight(self._clock.now())
            job.park(until, "quotaExceeded: parked until Pacific-midnight reset")
            raise _WorkflowStopped
        except UploadLimitExceededError as exc:
            job.transition(
                PublishState.FAILED_TERMINAL,
                f"uploadLimitExceeded: terminal for the day ({exc})",
            )
            raise _WorkflowStopped

    # -- phases -----------------------------------------------------------------

    def run(self, manifest: PublishManifest, data: bytes) -> PublishJob:
        """Full publish attempt. Returns the job in whatever state the
        failure matrix dictated; happy path ends POST_LIVE_DONE
        (immediate) or SCHEDULED (scheduled — call release() at the slot).
        """
        job = PublishJob(manifest=manifest)
        try:
            self._preflight(job, manifest)
            self._upload_enrich_process(job, manifest, data)
            if job.state is not PublishState.CHECKS_PASSED:
                return job
            if manifest.release_mode is ReleaseMode.SCHEDULED:
                decision = self.compliance_decision("public")
                if decision is not None and not decision["passed"]:
                    # Arming publishAt IS a public publish attempt:
                    # refused pre-PASS.  The upload stays private at
                    # CHECKS_PASSED with the typed reason recorded.
                    self._apply_compliance_hold(job)
                    return job
                try:
                    self._call(
                        job,
                        lambda: self._scheduler.schedule(
                            job, manifest, self._clock.now()
                        ),
                    )
                except _WorkflowStopped:
                    raise
                except Exception as exc:  # NoSlotAvailable / verify failure
                    if job.state is PublishState.CHECKS_PASSED:
                        job.transition(
                            PublishState.BLOCKED_HUMAN,
                            f"scheduling failed: {exc}",
                        )
                return job
            try:
                self.release(job, manifest)
            except ComplianceGateError:
                # Upload completed as a forced-private hold at
                # CHECKS_PASSED; the typed refusal is already
                # recorded on the job by release().
                return job
        except _WorkflowStopped:
            return job
        return job

    def _preflight(self, job: PublishJob, manifest: PublishManifest) -> None:
        # G0: manifest frozen, PASS tokens bound, ledger 100%.
        if not manifest.is_frozen:
            problems = manifest.gate_problems() or ["manifest not frozen"]
            job.transition(
                PublishState.BLOCKED_HUMAN,
                "G0 failed: " + "; ".join(problems),
            )
            raise _WorkflowStopped
        # Compliance Gate: from the very first byte the upload is
        # private; pre-PASS that is a FORCED private, recorded on the
        # job so every downstream report can disclose it.
        decision = self.compliance_decision("public")
        if decision is not None and not decision["passed"]:
            job.compliance_forced_private = True
        job.transition(PublishState.PREFLIGHT_OK, "G0/G1 preflight passed")

    def _upload_enrich_process(
        self, job: PublishJob, manifest: PublishManifest, data: bytes
    ) -> None:
        dead_reuploads = 0
        while True:
            result = self._call(job, lambda: self._uploader.upload(job, manifest, data))
            if job.state is PublishState.PREFLIGHT_OK:
                # Ledger hit: upload already completed in an earlier run.
                for st, why in (
                    (PublishState.SESSION_OPEN, "ledger hit"),
                    (PublishState.UPLOADING, "ledger hit"),
                    (PublishState.UPLOADED, "video_id already in ledger"),
                ):
                    job.transition(st, why)
            job.video_id = result.video_id

            # STEP 4 — enrichment, dependency order, while processing runs.
            job.transition(PublishState.ENRICHING, "applying thumbnail/captions/playlist")
            self._call(job, lambda: self._provider.set_thumbnail(
                result.video_id, manifest.thumbnail_path))
            self._call(job, lambda: self._provider.insert_captions(
                result.video_id, manifest.caption_path, manifest.caption_language))
            if manifest.playlist_id:
                self._call(job, lambda: self._provider.add_to_playlist(
                    manifest.playlist_id, result.video_id))

            # STEP 5/G3 — processing poll.
            job.transition(PublishState.PROCESSING, "polling processing status")
            poll = self._call(job, lambda: self._poller.poll(job, manifest))
            if poll.outcome is PollOutcome.CHECKS_PASSED:
                return
            if poll.outcome is PollOutcome.DEAD_UPLOAD and (
                dead_reuploads < self._max_dead_upload_reuploads
            ):
                dead_reuploads += 1
                self._retire_dead_video(job)
                job.transition(
                    PublishState.PREFLIGHT_OK,
                    "re-uploading dead upload as a NEW video",
                )
                continue
            if poll.outcome is PollOutcome.DEAD_UPLOAD:
                job.transition(
                    PublishState.BLOCKED_HUMAN,
                    "dead upload (P0D) and re-upload budget exhausted",
                )
            # Other outcomes already transitioned the job in the poller.
            return

    def _retire_dead_video(self, job: PublishJob) -> None:
        """Dead-upload procedure: record the old ID (it stays private),
        free the ledger, and let the loop re-upload as a NEW video so
        every downstream reference points at the new ID."""
        assert job.video_id is not None
        job.dead_video_ids.append(job.video_id)
        self._store.clear_video_id(job.idempotency_key)
        job.video_id = None
        job.session_uri = None
        job.bytes_confirmed = 0
        job.transition(
            PublishState.FAILED_RETRYABLE,
            f"dead upload retired: {job.dead_video_ids[-1]}",
        )

    # -- release + verification (G4/G5) -----------------------------------------

    def release(self, job: PublishJob, manifest: PublishManifest) -> PublishJob:
        """Flip public (immediate, or at the armed slot) and verify live.

        Nothing is announced as published until LIVE_CONFIRMED.
        """
        if job.state not in (PublishState.CHECKS_PASSED, PublishState.SCHEDULED):
            raise ValueError(f"cannot release from state {job.state}")
        if job.video_id is None:
            raise ValueError("cannot release without a video_id")
        # Gate C (Round 6): a public release while compliance is not
        # PASSED is REFUSED with the typed compliance reason — the
        # video stays private and no provider call is made.
        decision = self.compliance_decision("public")
        if decision is not None and not decision["passed"]:
            raise self._apply_compliance_hold(job)
        try:
            job.transition(PublishState.RELEASING, "release gate G4 passed")
            status = self._call(
                job, lambda: self._provider.get_video_status(job.video_id)  # type: ignore[arg-type]
            )
            if status.privacy_status != PRIVACY_PUBLIC:
                self._call(
                    job,
                    lambda: self._provider.update_video(
                        job.video_id, build_release_update()  # type: ignore[arg-type]
                    ),
                )
            job.armed_slot = None
            job.transition(PublishState.LIVE, "privacy is public; verifying")
            if self.verify_live(job, manifest):
                job.transition(PublishState.LIVE_CONFIRMED, "G5 verification passed")
                job.transition(PublishState.POST_LIVE_DONE, "publish complete")
        except _WorkflowStopped:
            return job
        return job

    def verify_live(self, job: PublishJob, manifest: PublishManifest) -> bool:
        """Post-live verification payload (G5). On mismatch the job is
        BLOCKED_HUMAN with the field-level diff — never silently fixed."""
        assert job.video_id is not None
        status = self._provider.get_video_status(job.video_id)
        checks = {
            "privacy_public": status.privacy_status == PRIVACY_PUBLIC,
            "title_byte_identical": status.title == manifest.title,
            "thumbnail_present": status.thumbnail_present,
            "caption_track_listed": (
                manifest.caption_language in status.caption_languages
            ),
            "hd_available": status.definition == "hd",
        }
        payload = {
            "video_id": job.video_id,
            "checks": checks,
            "remote_title": status.title,
            "published_at": (
                status.published_at.isoformat() if status.published_at else None
            ),
        }
        job.verification = payload
        if all(checks.values()):
            job.published_at = status.published_at or self._clock.now()
            return True
        failed = [k for k, ok in checks.items() if not ok]
        job.transition(
            PublishState.BLOCKED_HUMAN,
            f"G5 post-live verification failed: {', '.join(failed)}",
        )
        return False
