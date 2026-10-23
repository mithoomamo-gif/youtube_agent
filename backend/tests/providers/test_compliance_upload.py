"""Compliance Gate on the real-shaped YouTube upload path (Round 6).

The resumable uploader already inserts always-private and detects
Google-side forced-private.  With a compliance checker wired and not
PASSED, the APP's gate forces the requested visibility to private
before any session opens, and the result discloses that forcing
(``compliance_forced_private`` + the overall state) while still
reporting what the caller originally requested.  After PASS, the
requested visibility flows through unchanged.
"""

from __future__ import annotations

import httpx

from app.providers.youtube import (
    InMemoryQuotaLedger,
    InMemoryUploadSessionStore,
    YouTubeClient,
)
from app.providers.youtube.upload import ResumableUploader

from .test_youtube_api import (
    CHANNEL_ID,
    _upload_handler,
    _video_resource,
    make_http,
    run,
    seed_token_manager,
)


def _upload(tmp_path, *, checker, applied="private", requested="public"):
    file_path = tmp_path / "small.mp4"
    file_path.write_bytes(b"\x01" * 1_000)

    def put_final(_range):
        return _video_resource(applied)

    handler, recorded = _upload_handler(
        applied_privacy=applied, put_script=[put_final]
    )

    async def go():
        http = make_http(handler)
        tm = seed_token_manager(http)
        client = YouTubeClient(
            ledger=InMemoryQuotaLedger(), token_manager=tm, http=http
        )
        uploader = ResumableUploader(
            client, InMemoryUploadSessionStore(), compliance_checker=checker
        )
        return await uploader.upload(
            file_path=file_path,
            channel_id=CHANNEL_ID,
            title="Compliance upload",
            category_id="22",
            self_declared_made_for_kids=False,
            contains_synthetic_media=False,
            requested_privacy=requested,
        )

    return run(go()), recorded


def test_upload_forced_private_by_compliance_gate_pre_pass(tmp_path):
    result, recorded = _upload(tmp_path, checker=lambda: "not_started")
    # Insert is private (as always) and the session's requested
    # visibility was forced to private by OUR gate: the completed
    # resource coming back private is therefore NOT a Google-side
    # forced-private surprise, it is the gate's doing — disclosed.
    assert recorded["posts"][0]["status"]["privacyStatus"] == "private"
    assert result.compliance_forced_private is True
    assert result.compliance_overall == "not_started"
    assert result.requested_privacy_status == "public"  # as requested
    assert result.applied_privacy_status == "private"
    # Not a Google-side forced private (the session never asked the
    # provider for public) — the forcing is ours, in its own field.
    assert result.forced_private is False


def test_upload_after_compliance_pass_honours_requested(tmp_path):
    result, _recorded = _upload(
        tmp_path, checker=lambda: "passed", applied="public"
    )
    assert result.compliance_forced_private is False
    assert result.compliance_overall == "passed"
    assert result.requested_privacy_status == "public"
    assert result.applied_privacy_status == "public"
    assert result.forced_private is False


def test_upload_without_checker_is_ungated(tmp_path):
    result, _recorded = _upload(tmp_path, checker=None)
    assert result.compliance_forced_private is False
    assert result.compliance_overall is None
