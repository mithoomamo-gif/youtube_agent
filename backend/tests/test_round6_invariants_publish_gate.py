"""ROUND 6 — Invariant 4 proof pack: the Upload path cannot run
without (i) a stored in-scope OAuth grant AND (ii) QA/compliance PASS
tokens bound to the frozen manifest hash.

Enforcement in this codebase is layered; each layer is proven at the
layer that actually enforces it:

* L1 ``PublishManifest.freeze()`` (G0) — typed ``ManifestError`` when
  any of the QC / Copyright / Policy PASS tokens is missing or the
  licence ledger is below 100%.
* L2 ``PublishingWorkflow`` preflight — an unfrozen manifest (which is
  what ANY post-freeze edit produces: ``derive_new_version`` clears
  every token and the hash) lands the job in ``BLOCKED_HUMAN`` and the
  provider is never touched.
* L3 ``pipeline.gates.verify_gate_b`` — ``GateToken`` objects must be
  PASS, intact (digest), and bound to the exact manifest hash:
  missing / mismatched / tampered / non-PASS are distinct typed
  problems.
* L4 orchestration Gate B + Gate C — PASS tokens from agents 32/33/34
  bound to the project manifest hash, and ONLY agent 26 (Upload) may
  pass Gate C.
* L5 OAuth grant — ``TokenManager`` raises the typed ``NeedsReauth``
  when no grant is stored (and the API client surfaces it BEFORE any
  HTTP), when the stored grant is marked ``needs_reauth``, and when a
  refresh is answered ``invalid_grant``.

One GAP characterisation test (marked ``__gap_r6_f5``) remains, plus
the R6-F4 closure:

* F4 (CLOSED in the closure pass): a stored grant whose scopes do
  NOT include the ``youtube.upload`` scope is now refused LOCALLY —
  typed ``NeedsReauth`` — before any upload HTTP, at the upload
  entry points (``ResumableUploader`` and ``videos.insert`` through
  ``YouTubeClient.request``). The flipped L5 test proves the refusal
  and that zero HTTP calls are made; non-upload reads are unaffected.
* F5: gate tokens carry ``issued_at`` but no age is ever checked —
  "stale" is enforced only through manifest-hash binding, never time.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx
import pytest

from app.orchestration.gates import (
    can_pass_gate_b,
    can_pass_gate_c,
    issue_pass_token,
)
from app.orchestration.state import GateStatus
from app.pipeline.gates.aggregator import verify_gate_b
from app.pipeline.gates.tokens import (
    GATE_COPYRIGHT,
    GATE_POLICY_SIMILARITY,
    GATE_QC_TECHNICAL,
    Verdict,
    issue_token,
)
from app.pipeline.publishing import (
    FakeClock,
    InMemoryStore,
    ManifestError,
    PublishManifest,
    PublishState,
    PublishingWorkflow,
)
from app.pipeline.publishing.provider import ChunkResult, VideoStatus
from app.providers.youtube import (
    InMemoryQuotaLedger,
    InMemorySecretStore,
    InMemoryUploadSessionStore,
    ResumableUploader,
    TokenManager,
    TokenResponse,
    YouTubeClient,
)
from app.providers.youtube.errors import NeedsReauth
from app.providers.youtube.oauth import CONNECT_SCOPES

DATA = b"x" * 2048
CHANNEL_ID = "UCr6channel00000000000"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _manifest_base(**over) -> dict:
    base = dict(
        project_id="proj-r6",
        channel_id=CHANNEL_ID,
        playlist_id="pl-r6",
        master_path="/tmp/master.mp4",
        master_checksum="deadbeefcafe",
        master_size_bytes=len(DATA),
        duration_seconds=600.0,
        resolution_width=1920,
        resolution_height=1080,
        thumbnail_path="/tmp/thumb.jpg",
        caption_path="/tmp/caps.srt",
        caption_language="en",
        title="Round Six Title",
        description="Intro\n00:00 Start",
        tags=["facts"],
        category_id="22",
        qc_pass_token="tok-qc",
        copyright_pass_token="tok-copyright",
        policy_pass_token="tok-policy",
        licence_ledger_completeness=1.0,
    )
    base.update(over)
    return base


def _frozen_manifest(**over) -> PublishManifest:
    return PublishManifest(**_manifest_base(**over)).freeze()


class ExplodingProvider:
    """Any call is a test failure: a refused publish touches nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _boom(self, name):
        self.calls.append(name)
        raise AssertionError(f"provider method {name} must not be called")

    def create_resumable_session(self, *a):
        self._boom("create_resumable_session")

    def upload_chunk(self, *a):
        self._boom("upload_chunk")

    def query_upload_status(self, *a):
        self._boom("query_upload_status")

    def set_thumbnail(self, *a):
        self._boom("set_thumbnail")

    def insert_captions(self, *a):
        self._boom("insert_captions")

    def add_to_playlist(self, *a):
        self._boom("add_to_playlist")

    def update_video(self, *a):
        self._boom("update_video")

    def get_video_status(self, *a):
        self._boom("get_video_status")


class HappyProvider:
    """Minimal in-memory provider that can carry a full publish."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.videos: dict[str, dict] = {}
        self._n = 0

    def create_resumable_session(self, metadata, file_size):
        assert metadata.privacy_status == "private"
        self._n += 1
        return f"session://{self._n}"

    def upload_chunk(self, session_uri, data, offset):
        vid = f"vid-{self._n}"
        self.videos[vid] = {
            "title": "Round Six Title", "privacy": "private",
            "published_at": None, "thumbnail": False, "captions": [],
        }
        return ChunkResult(201, offset + len(data), vid)

    def query_upload_status(self, session_uri):
        return ChunkResult(201, len(DATA), f"vid-{self._n}")

    def set_thumbnail(self, video_id, thumbnail_path):
        self.videos[video_id]["thumbnail"] = True

    def insert_captions(self, video_id, srt_path, language):
        self.videos[video_id]["captions"].append(language)
        return "cap-1"

    def add_to_playlist(self, playlist_id, video_id):
        return "pli-1"

    def update_video(self, video_id, update):
        rec = self.videos[video_id]
        if update.privacy_status is not None:
            rec["privacy"] = update.privacy_status
        if rec["privacy"] == "public" and rec["published_at"] is None:
            rec["published_at"] = self.clock.now()

    def get_video_status(self, video_id):
        rec = self.videos[video_id]
        return VideoStatus(
            video_id=video_id,
            privacy_status=rec["privacy"],
            published_at=rec["published_at"],
            upload_status="processed",
            processing_status="succeeded",
            duration_seconds=600.0,
            definition="hd",
            title=rec["title"],
            thumbnail_present=rec["thumbnail"],
            caption_languages=list(rec["captions"]),
        )


def _workflow(provider, clock) -> PublishingWorkflow:
    return PublishingWorkflow(provider, InMemoryStore(), clock=clock)


# ---------------------------------------------------------------------------
# L1 — manifest freeze refuses (typed) without every PASS token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["qc_pass_token", "copyright_pass_token", "policy_pass_token"]
)
def test_l1_freeze_refuses_when_any_pass_token_missing(field):
    with pytest.raises(ManifestError) as exc:
        PublishManifest(**_manifest_base(**{field: None})).freeze()
    assert "PASS token" in str(exc.value)


def test_l1_freeze_refuses_when_licence_ledger_incomplete():
    with pytest.raises(ManifestError) as exc:
        PublishManifest(
            **_manifest_base(licence_ledger_completeness=0.99)
        ).freeze()
    assert "licence ledger" in str(exc.value)


def test_l1_freeze_succeeds_with_all_tokens_and_full_ledger():
    frozen = _frozen_manifest()
    assert frozen.is_frozen and frozen.manifest_hash


# ---------------------------------------------------------------------------
# L2 — workflow preflight: unfrozen == BLOCKED_HUMAN, provider untouched
# ---------------------------------------------------------------------------


def test_l2_workflow_blocks_unfrozen_manifest_without_touching_provider():
    clock = FakeClock()
    provider = ExplodingProvider()
    manifest = PublishManifest(
        **_manifest_base(
            qc_pass_token=None, copyright_pass_token=None,
            policy_pass_token=None, licence_ledger_completeness=0.0,
        )
    )
    job = _workflow(provider, clock).run(manifest, DATA)
    assert job.state is PublishState.BLOCKED_HUMAN
    assert provider.calls == []


def test_l2_any_post_freeze_edit_invalidates_tokens_and_blocks():
    """Stale-after-edit: editing a frozen manifest derives v2 with all
    tokens cleared — the old PASS evidence cannot publish the new cut."""
    clock = FakeClock()
    provider = ExplodingProvider()
    edited = _frozen_manifest().derive_new_version(title="A Different Title")
    assert edited.is_frozen is False
    assert edited.qc_pass_token is None
    assert edited.copyright_pass_token is None
    assert edited.policy_pass_token is None
    problems = edited.gate_problems()
    assert sum("PASS token" in p for p in problems) == 3
    job = _workflow(provider, clock).run(edited, DATA)
    assert job.state is PublishState.BLOCKED_HUMAN
    assert provider.calls == []


def test_l2_control_frozen_manifest_publishes_end_to_end():
    """Control: the refusal tests above are not vacuous — a properly
    frozen manifest DOES flow through the same workflow to done."""
    clock = FakeClock()
    provider = HappyProvider(clock)
    job = _workflow(provider, clock).run(_frozen_manifest(), DATA)
    assert job.state is PublishState.POST_LIVE_DONE


# ---------------------------------------------------------------------------
# L3 — pipeline Gate B tokens: binding, tamper, verdict
# ---------------------------------------------------------------------------

_HASH = "a" * 64
_OTHER_HASH = "b" * 64


def _gate_tokens(manifest_hash=_HASH, qc_verdict=Verdict.PASS):
    return [
        issue_token(GATE_QC_TECHNICAL, qc_verdict, manifest_hash),
        issue_token(GATE_COPYRIGHT, Verdict.PASS, manifest_hash),
        issue_token(GATE_POLICY_SIMILARITY, Verdict.PASS, manifest_hash),
    ]


def test_l3_gate_b_allows_three_bound_pass_tokens():
    result = verify_gate_b(_gate_tokens(), _HASH)
    assert result.allowed and result.problems == ()


@pytest.mark.parametrize("drop", [0, 1, 2])
def test_l3_gate_b_refuses_when_any_token_missing(drop):
    tokens = _gate_tokens()
    del tokens[drop]
    result = verify_gate_b(tokens, _HASH)
    assert not result.allowed
    assert any(p.code == "MISSING_TOKEN" for p in result.problems)


def test_l3_gate_b_refuses_tokens_bound_to_a_different_manifest():
    result = verify_gate_b(_gate_tokens(_OTHER_HASH), _HASH)
    assert not result.allowed
    assert any(p.code == "MANIFEST_HASH_MISMATCH" for p in result.problems)


def test_l3_gate_b_refuses_tampered_token():
    tokens = _gate_tokens()
    tokens[0] = dataclasses.replace(tokens[0], verdict=Verdict.BLOCK)
    result = verify_gate_b(tokens, _HASH)
    assert not result.allowed
    assert any(p.code == "TOKEN_TAMPERED" for p in result.problems)


def test_l3_gate_b_refuses_non_pass_verdict_with_valid_digest():
    result = verify_gate_b(_gate_tokens(qc_verdict=Verdict.WARN), _HASH)
    assert not result.allowed
    assert any(p.code == "VERDICT_NOT_PASS" for p in result.problems)


def test_l3_gate_b_tokens_for_previous_manifest_version_are_stale():
    """Hash-staleness IS enforced: v1 tokens never satisfy v2's Gate B."""
    v1_tokens = _gate_tokens(_HASH)
    assert verify_gate_b(v1_tokens, _HASH).allowed
    assert not verify_gate_b(v1_tokens, _OTHER_HASH).allowed


def test_l3_gate_b_token_age_is_not_checked__gap_r6_f5():
    """GAP R6-F5 (characterisation): a token issued in 2020 still
    satisfies Gate B today — ``issued_at`` is recorded but no maximum
    age exists anywhere in the verification path. 'Stale' is enforced
    only via manifest-hash binding (proven above), never via time.
    If an age limit is ever added, this test must be flipped."""
    old = [
        issue_token(GATE_QC_TECHNICAL, Verdict.PASS, _HASH,
                    issued_at="2020-01-01T00:00:00Z"),
        issue_token(GATE_COPYRIGHT, Verdict.PASS, _HASH,
                    issued_at="2020-01-01T00:00:00Z"),
        issue_token(GATE_POLICY_SIMILARITY, Verdict.PASS, _HASH,
                    issued_at="2020-01-01T00:00:00Z"),
    ]
    assert verify_gate_b(old, _HASH).allowed, (
        "an age check appeared in verify_gate_b — update the R6-F5 "
        "finding in docs/ROUND6_INVARIANTS.md and this test together"
    )


# ---------------------------------------------------------------------------
# L4 — orchestration Gate B + Gate C (only agent 26 may publish)
# ---------------------------------------------------------------------------

_MH = "manifest-hash-r6"


def _orch_state(tokens, manifest_hash=_MH, gate_a=True):
    gate_status = GateStatus(
        gate_a_script_locked=gate_a,
        gate_a_script_hash="script-hash-r6",
        gate_b_tokens=list(tokens),
        gate_b_passed=False,
    )
    return {
        "locked_script_ref": "locked-r6",
        "locked_script_hash": "script-hash-r6",
        "script_versions": ["locked-r6"],
        "red_claim_count": 0,
        "retention_lint_passed": True,
        "publish_manifest_hash": manifest_hash,
        "gate_status": gate_status.model_dump(mode="json"),
    }


def _all_orch_tokens(manifest_hash=_MH):
    return [issue_pass_token(aid, manifest_hash) for aid in (32, 33, 34)]


def test_l4_gate_c_allows_upload_agent_after_gate_b():
    state = _orch_state(_all_orch_tokens())
    ok, reasons = can_pass_gate_c(state, 26)
    assert ok, reasons


@pytest.mark.parametrize("agent_id", [1, 25, 27, 32, 35])
def test_l4_gate_c_refuses_every_non_upload_agent(agent_id):
    state = _orch_state(_all_orch_tokens())
    ok, reasons = can_pass_gate_c(state, agent_id)
    assert not ok
    assert any("26" in r and "Upload" in r for r in reasons)


def test_l4_gate_c_refuses_upload_agent_when_gate_b_incomplete():
    state = _orch_state(_all_orch_tokens()[:2])  # agent 34 missing
    ok, reasons = can_pass_gate_c(state, 26)
    assert not ok
    assert any("Gate B" in r for r in reasons)


def test_l4_gate_c_refuses_when_tokens_bound_to_other_manifest():
    state = _orch_state(_all_orch_tokens("some-other-manifest-hash"))
    ok_b, _ = can_pass_gate_b(state)
    assert not ok_b
    ok_c, _ = can_pass_gate_c(state, 26)
    assert not ok_c


def test_l4_gate_c_refuses_when_gate_a_never_passed():
    state = _orch_state(_all_orch_tokens(), gate_a=False)
    ok, _ = can_pass_gate_c(state, 26)
    assert not ok


# ---------------------------------------------------------------------------
# L5 — OAuth grant: typed refusal without a stored, valid grant
# ---------------------------------------------------------------------------


def _counting_http(calls: list, response: httpx.Response | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return response or httpx.Response(200, json={"items": []})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _token_manager(http) -> TokenManager:
    return TokenManager(
        InMemorySecretStore(),
        client_id="r6-client-id",
        client_secret="r6-client-secret",
        http=http,
    )


def _seed(tm: TokenManager, scopes: str) -> None:
    tm.store_token_response(
        CHANNEL_ID,
        TokenResponse(
            access_token="seed-access-token",
            refresh_token="seed-refresh-token",
            expires_in=3600,
            scope=scopes,
        ),
    )


def test_l5_no_stored_grant_raises_needs_reauth_typed():
    calls: list[str] = []
    tm = _token_manager(_counting_http(calls))
    with pytest.raises(NeedsReauth):
        run(tm.get_access_token(CHANNEL_ID))
    assert calls == []  # refusal happens before any HTTP


def test_l5_no_stored_grant_api_client_refuses_before_any_http():
    calls: list[str] = []
    http = _counting_http(calls)
    tm = _token_manager(http)
    client = YouTubeClient(
        ledger=InMemoryQuotaLedger(), token_manager=tm, http=http
    )
    with pytest.raises(NeedsReauth):
        run(client.request("videos.list", channel_id=CHANNEL_ID))
    assert calls == []


def test_l5_needs_reauth_blob_refuses_without_http():
    calls: list[str] = []
    tm = _token_manager(_counting_http(calls))
    tm._store.put(
        f"youtube:channel:{CHANNEL_ID}",
        json.dumps(
            {
                "refresh_token": "seed-refresh-token",
                "scopes": list(CONNECT_SCOPES),
                "obtained_at": 1.0,
                "status": "needs_reauth",
                "testing_mode_token": False,
            }
        ),
    )
    with pytest.raises(NeedsReauth):
        run(tm.refresh(CHANNEL_ID))
    assert calls == []


def test_l5_invalid_grant_on_refresh_flips_channel_to_needs_reauth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tm = _token_manager(http)
    _seed(tm, " ".join(CONNECT_SCOPES))
    tm._cache.clear()  # force the refresh path
    with pytest.raises(NeedsReauth):
        run(tm.refresh(CHANNEL_ID))
    assert tm.get_status(CHANNEL_ID).status == "needs_reauth"


def test_l5_control_in_scope_grant_yields_access_token():
    calls: list[str] = []
    tm = _token_manager(_counting_http(calls))
    _seed(tm, " ".join(CONNECT_SCOPES))
    assert run(tm.get_access_token(CHANNEL_ID)) == "seed-access-token"
    status = tm.get_status(CHANNEL_ID)
    assert set(CONNECT_SCOPES) <= set(status.scopes)


def test_l5_out_of_scope_grant_is_refused_locally_on_upload(tmp_path):
    """R6-F4 (CLOSED): a grant holding ONLY the readonly scope — not
    the ``youtube.upload`` scope requested at connect time — is
    refused LOCALLY with the typed ``NeedsReauth`` BEFORE any upload
    HTTP, at both upload entry points: ``videos.insert`` through
    ``YouTubeClient.request`` and ``ResumableUploader.upload``. Zero
    HTTP calls are made (asserted via the counting mock transport).
    The gate is upload-scoped, not global: the same grant still
    yields a token and a successful non-upload read, exactly as
    before — readonly grants remain usable for reads."""
    calls: list[str] = []
    http = _counting_http(calls)
    tm = _token_manager(http)
    readonly = "https://www.googleapis.com/auth/youtube.readonly"
    _seed(tm, readonly)
    status = tm.get_status(CHANNEL_ID)
    assert not (set(CONNECT_SCOPES) & set(status.scopes)), (
        "seed unexpectedly overlaps the upload scopes"
    )
    client = YouTubeClient(
        ledger=InMemoryQuotaLedger(), token_manager=tm, http=http
    )
    # Upload via the API client: typed refusal, zero HTTP.
    with pytest.raises(NeedsReauth):
        run(
            client.request(
                "videos.insert", channel_id=CHANNEL_ID, http_method="POST"
            )
        )
    assert calls == []
    # Upload via the resumable uploader: typed refusal, zero HTTP.
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 100)
    uploader = ResumableUploader(client, InMemoryUploadSessionStore())
    with pytest.raises(NeedsReauth):
        run(
            uploader.upload(
                file_path=video,
                channel_id=CHANNEL_ID,
                title="Round Six Title",
                category_id="22",
                self_declared_made_for_kids=False,
                contains_synthetic_media=False,
            )
        )
    assert calls == []
    # Control: non-upload reads on the same grant are NOT refused.
    assert run(tm.get_access_token(CHANNEL_ID)) == "seed-access-token"
    resp = run(client.request("videos.list", channel_id=CHANNEL_ID))
    assert resp.status_code == 200
    assert len(calls) == 1
