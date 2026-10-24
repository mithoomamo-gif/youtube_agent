"""Style Lab end-to-end intake flow through the real API.

One full pass, no service/router patching:

    create two owner channels
      -> add a competitor by @handle URL under channel A
      -> attach a tiny REAL mp4 (ffmpeg lavfi ``testsrc``, generated
         in-test) with a REAL thumbnail (frame extracted by ffmpeg)
      -> paste a second video's metadata
      -> metadata/fetch with NO YouTube credential configured returns
         the honest ``metadata_unavailable`` state (nothing is mocked:
         the credential env is cleared and the outcome is asserted to
         be one of the honest states, with the observed state recorded
         in the assertion message)
      -> queue analysis on the attached video (status ``queued``)
      -> create a style profile from the competitor
      -> activate it; a second profile's activation supersedes it.

Channel B exists throughout as the isolation witness: its Style Lab
lists stay empty for the whole flow.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SUFFIX = uuid.uuid4().hex[:10]
CHAN_A = f"sl-e2e-a-{_SUFFIX}"
CHAN_B = f"sl-e2e-b-{_SUFFIX}"
YT_A = f"UCe2eA{_SUFFIX}xxxx"
YT_B = f"UCe2eB{_SUFFIX}xxxx"
HANDLE = f"E2eRival{_SUFFIX[:8]}"

HONEST_METADATA_STATES = ("fetched", "metadata_unavailable", "fetch_failed")

# The step-6 profile payload is the Round-3 proof profile (patterns
# only). A byte-identical copy is committed as a test fixture so this
# file also passes in clean checkouts — outputs/ is fully gitignored
# and never reaches CI. The fixture is preferred; the live proof
# artifact stays as the fallback on dev machines if the fixture is
# ever removed. (Same pattern as test_stylelab_profile_boundary.py.)
_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures" / "stylelab" / "style_profile.json"
)
_OUTPUTS_PROOF_PATH = (
    Path(__file__).resolve().parents[2]
    / "outputs" / "stylelab" / "proof" / "style_profile.json"
)
_PROOF_PATH = (
    _FIXTURE_PATH if _FIXTURE_PATH.is_file() else _OUTPUTS_PROOF_PATH
)


def _seed_channels() -> None:
    from app import db as db_module

    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        for chan_id, yt_id, title in (
            (CHAN_A, YT_A, "E2E Owner A"),
            (CHAN_B, YT_B, "E2E Owner B"),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO channels (id, youtube_channel_id,"
                " title, language, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'en', 'active', ?, ?)",
                (chan_id, yt_id, title, now, now),
            )
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _channels(app):
    _seed_channels()
    yield


def _ffmpeg(*args: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not available for media fixtures")
    proc = subprocess.run([ffmpeg, "-y", *args],
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-500:]


def test_style_lab_full_intake_flow(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.config import get_settings

    # -- 0. B starts (and stays) empty --------------------------------
    assert client.get(
        f"/api/v1/channels/{CHAN_B}/competitors").json()["items"] == []
    assert client.get(
        f"/api/v1/channels/{CHAN_B}/style-profiles").json()["items"] == []

    # -- 1. Competitor by @handle URL ----------------------------------
    comp_resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors",
        json={"input": f"https://www.youtube.com/@{HANDLE}"},
    )
    assert comp_resp.status_code == 201, comp_resp.text
    competitor = comp_resp.json()
    assert competitor["channel_id"] == CHAN_A
    assert competitor["handle"] == HANDLE
    assert competitor["source_kind"] == "channel_url"
    assert competitor["metadata_status"] == "not_fetched"
    comp_id = competitor["id"]

    # -- 2. Attach a tiny real mp4 (testsrc) + thumbnail ---------------
    clip = tmp_path / "e2e-clip.mp4"
    _ffmpeg("-f", "lavfi", "-i",
            "testsrc=duration=1:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", str(clip))
    thumb = tmp_path / "e2e-thumb.jpg"
    _ffmpeg("-i", str(clip), "-frames:v", "1", str(thumb))
    clip_bytes = clip.read_bytes()
    assert clip_bytes

    attach_resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp_id}/videos/attach",
        files={
            "video": ("e2e-clip.mp4", clip_bytes, "video/mp4"),
            "thumbnail": ("e2e-thumb.jpg", thumb.read_bytes(), "image/jpeg"),
        },
        data={"title": "E2E Attached Rival Video",
              "description": "user-supplied competitor clip"},
    )
    assert attach_resp.status_code == 201, attach_resp.text
    attached = attach_resp.json()
    assert attached["source"] == "attached_file"
    assert attached["competitor_channel_id"] == comp_id
    assert attached["duration_s"] == pytest.approx(1.0, abs=0.35)
    assert attached["width"] == 320 and attached["height"] == 240
    assert attached["mime_type"] == "video/mp4"
    assert attached["file_size_bytes"] == len(clip_bytes)
    assert attached["file_sha256"] == hashlib.sha256(clip_bytes).hexdigest()
    assert attached["analysis_status"] == "not_queued"

    data_dir = Path(get_settings().data_dir)
    stored_video = data_dir / attached["storage_path"]
    assert stored_video.exists()
    assert hashlib.sha256(stored_video.read_bytes()).hexdigest() == (
        attached["file_sha256"])
    assert attached["thumbnail_storage_path"]
    assert len(attached["thumbnail_sha256"]) == 64
    assert (data_dir / attached["thumbnail_storage_path"]).exists()

    # -- 3. Paste a second video's metadata ----------------------------
    paste_resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp_id}/videos",
        json={
            "source": "pasted_metadata",
            "title": "E2E Pasted Rival Video",
            "description": "Title/description the user pasted by hand.",
            "tags": ["e2e", "style-lab"],
        },
    )
    assert paste_resp.status_code == 201, paste_resp.text
    pasted = paste_resp.json()
    assert pasted["source"] == "pasted_metadata"
    assert pasted["tags"] == ["e2e", "style-lab"]
    assert pasted["storage_path"] is None
    assert pasted["analysis_status"] == "not_queued"

    videos = client.get(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp_id}/videos"
    ).json()["items"]
    assert [v["id"] for v in videos] == [attached["id"], pasted["id"]]

    # -- 4. Metadata fetch: honest state, no credential ----------------
    # Determinism: make sure no API key leaks in from the environment.
    # (The OAuth *client* id/secret in the dev .env are NOT a Data API
    # credential and are never consulted by the metadata gate.)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    fetch_resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp_id}/metadata/fetch"
    )
    assert fetch_resp.status_code == 200, fetch_resp.text
    fetched = fetch_resp.json()
    observed_state = fetched["metadata_status"]
    assert observed_state in HONEST_METADATA_STATES, (
        f"metadata/fetch returned a non-honest state {observed_state!r}")
    assert observed_state == "metadata_unavailable", (
        "expected 'metadata_unavailable' with no YouTube credential "
        f"configured in the test env; observed {observed_state!r} — if "
        "the harness now configures a credential, this expectation is "
        "the thing to revisit, not the honesty contract")
    assert fetched["fetch_status"] == "metadata_unavailable"
    # Persisted on the row, not just reported in the response.
    got = client.get(f"/api/v1/channels/{CHAN_A}/competitors/{comp_id}")
    assert got.json()["metadata_status"] == "metadata_unavailable"

    # -- 5. Queue analysis on the attached video ------------------------
    queue_resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitor-videos/{attached['id']}"
        "/analysis"
    )
    assert queue_resp.status_code == 200, queue_resp.text
    queued = queue_resp.json()
    assert queued["analysis_status"] == "queued"
    assert queued["analysis_requested_at"] is not None

    videos = client.get(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp_id}/videos"
    ).json()["items"]
    by_id = {v["id"]: v for v in videos}
    assert by_id[attached["id"]]["analysis_status"] == "queued"
    assert by_id[pasted["id"]]["analysis_status"] == "not_queued"

    # -- 6. Style profile from the competitor, then activate ------------
    # SL-001: the profile payload must be a valid style_profile.v1
    # (patterns only). The pre-fix free-form payload this test used to
    # post ({"hook_pattern": ..., "pacing": ...}) is exactly the
    # arbitrary-JSON behaviour SL-001 closed; the payload below is the
    # proof profile shape, re-scoped to this channel, and the persisted
    # profile is the schema's canonical dump, which round-trips exactly.
    profile_payload = json.loads(_PROOF_PATH.read_text(encoding="utf-8"))
    profile_payload["channel_id"] = CHAN_A
    p1_resp = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles",
        json={"name": "E2E Rival Style v1",
              "competitor_ids": [comp_id],
              "profile": profile_payload},
    )
    assert p1_resp.status_code == 201, p1_resp.text
    p1 = p1_resp.json()
    assert p1["status"] == "draft"
    assert p1["channel_id"] == CHAN_A
    assert p1["competitor_ids"] == [comp_id]
    assert p1["profile"] == profile_payload

    act1 = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles/{p1['id']}/activate")
    assert act1.status_code == 200, act1.text
    assert act1.json()["status"] == "active"

    # -- 7. Second profile activation supersedes the first --------------
    p2_resp = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles",
        json={"name": "E2E Rival Style v2", "competitor_ids": [comp_id]},
    )
    assert p2_resp.status_code == 201, p2_resp.text
    p2 = p2_resp.json()
    act2 = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles/{p2['id']}/activate")
    assert act2.status_code == 200, act2.text
    assert act2.json()["status"] == "active"

    listing = client.get(f"/api/v1/channels/{CHAN_A}/style-profiles")
    by_id = {p["id"]: p for p in listing.json()["items"]}
    assert by_id[p1["id"]]["status"] == "superseded"
    assert by_id[p2["id"]]["status"] == "active"
    active = [p for p in by_id.values() if p["status"] == "active"]
    assert len(active) == 1

    # -- 8. B never saw any of it ---------------------------------------
    assert client.get(
        f"/api/v1/channels/{CHAN_B}/competitors").json()["items"] == []
    assert client.get(
        f"/api/v1/channels/{CHAN_B}/style-profiles").json()["items"] == []
