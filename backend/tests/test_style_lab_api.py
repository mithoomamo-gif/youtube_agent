"""Style Lab REST API tests (intake + storage round).

Full endpoint matrix for ``app.routers.style_lab`` +
``app.style_lab.service`` over the sibling contract modules
(parsing / store / schema / metadata).  Uses the conftest ``app`` /
``client`` fixtures (full sidecar app, launch-token header set) like
``tests/test_channels_connect.py``.

Covered:
- create competitor by channel URL + by @handle (+ handle URL form);
  bad host / garbage input rejected 422 with a machine-readable
  reason; unknown owner channel 404;
- owner scoping: EVERY scoped endpoint answers 404 (never 403) when
  the competitor / video / profile lives under another channel;
- file attach: a tiny REAL mp4 (ffmpeg lavfi) lands with probed
  duration / dimensions / sha256 / size; an invalid file is a 422
  with a machine-readable reason and leaves no row behind;
- metadata attach (pasted + URL) works with NO fetch involved;
- metadata/fetch with the metadata module boundary patched: no
  credential -> 200 ``metadata_unavailable``; a fetched result fills
  display_name / external_channel_id (no live HTTP anywhere);
- analysis queue flips ``analysis_status`` to ``queued``;
- style profiles: create (cross-channel competitor -> 4xx), list
  scoping, activate supersedes the previously active profile.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SUFFIX = uuid.uuid4().hex[:10]
CHAN_A = f"sl-a-{_SUFFIX}"
CHAN_B = f"sl-b-{_SUFFIX}"
YT_A = f"UCslA{_SUFFIX}xxxx"
YT_B = f"UCslB{_SUFFIX}xxxx"
RIVAL_CHANNEL_ID = "UC" + "r" * 22  # valid UC + 22 form for the parser
VIDEO_ID = "aBcDeFgHiJk"  # 11-char video id form


# ---------------------------------------------------------------------------
# Seeding / helpers
# ---------------------------------------------------------------------------


def _seed_channels() -> None:
    from app import db as db_module

    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        for chan_id, yt_id, title in (
            (CHAN_A, YT_A, "Owner Channel A"),
            (CHAN_B, YT_B, "Owner Channel B"),
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
    # Depends on the conftest `app` fixture so the DB schema exists.
    _seed_channels()
    yield


def _create_competitor(client: TestClient, channel_id: str, raw: str, **kw):
    body = {"input": raw}
    body.update(kw)
    return client.post(f"/api/v1/channels/{channel_id}/competitors", json=body)


def _must_competitor(client: TestClient, channel_id: str, raw: str) -> dict:
    resp = _create_competitor(client, channel_id, raw)
    assert resp.status_code == 201, resp.text
    return resp.json()


_MEDIA_CACHE: dict[str, bytes] = {}


def _media_bytes(kind: str, tmp_path: Path) -> bytes:
    if kind in _MEDIA_CACHE:
        return _MEDIA_CACHE[kind]
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not available for media fixtures")
    if kind == "mp4":
        out = tmp_path / "clip.mp4"
        cmd = [ffmpeg, "-y", "-f", "lavfi", "-i",
               "color=c=red:s=64x64:d=1:r=10", "-pix_fmt", "yuv420p",
               str(out)]
    else:  # jpg
        out = tmp_path / "thumb.jpg"
        cmd = [ffmpeg, "-y", "-f", "lavfi", "-i",
               "color=c=blue:s=32x32:d=0.1", "-frames:v", "1", str(out)]
    proc = subprocess.run(cmd, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-500:]
    data = out.read_bytes()
    assert data
    _MEDIA_CACHE[kind] = data
    return data


# ---------------------------------------------------------------------------
# Competitor creation: URL / handle / rejections
# ---------------------------------------------------------------------------


def test_create_competitor_by_channel_url(client: TestClient):
    resp = _create_competitor(
        client, CHAN_A, f"https://www.youtube.com/channel/{RIVAL_CHANNEL_ID}"
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["channel_id"] == CHAN_A
    assert body["source_kind"] == "channel_url"
    assert body["external_channel_id"] == RIVAL_CHANNEL_ID
    assert body["channel_url"] == (
        f"https://www.youtube.com/channel/{RIVAL_CHANNEL_ID}"
    )
    assert body["metadata_status"] == "not_fetched"
    assert body["status"] == "active"
    assert body["platform"] == "youtube"

    listing = client.get(f"/api/v1/channels/{CHAN_A}/competitors")
    assert listing.status_code == 200
    assert [i["id"] for i in listing.json()["items"]] == [body["id"]]

    got = client.get(f"/api/v1/channels/{CHAN_A}/competitors/{body['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == body["id"]


def test_create_competitor_by_handle_and_handle_url(client: TestClient):
    by_handle = _must_competitor(client, CHAN_A, "@RivalHandle")
    assert by_handle["source_kind"] == "handle"
    assert by_handle["handle"] == "RivalHandle"  # '@' stripped, case kept
    assert by_handle["channel_url"] == "https://www.youtube.com/@RivalHandle"

    by_url = _must_competitor(
        client, CHAN_A, "https://www.youtube.com/@SecondRival"
    )
    assert by_url["source_kind"] == "channel_url"
    assert by_url["handle"] == "SecondRival"


def test_create_competitor_by_video_url_keeps_channel_unresolved(
    client: TestClient,
):
    body = _must_competitor(
        client, CHAN_A, f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )
    assert body["source_kind"] == "video_url"
    # A video URL alone cannot identify its channel (parser contract).
    assert body["external_channel_id"] is None
    assert body["handle"] is None


def test_create_competitor_rejects_bad_host(client: TestClient):
    resp = _create_competitor(client, CHAN_A, "https://vimeo.com/channels/1")
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "invalid_competitor_input"
    assert error["details"]["reason"] == "not_youtube_host"

    garbage = _create_competitor(client, CHAN_A, "not a channel at all!!")
    assert garbage.status_code == 422
    assert garbage.json()["error"]["code"] == "invalid_competitor_input"
    assert garbage.json()["error"]["details"]["reason"]


def test_create_competitor_unknown_owner_channel_404(client: TestClient):
    resp = _create_competitor(client, "no-such-channel", "@RivalHandle")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "channel_not_found"


def test_create_competitor_dedupes_same_handle(client: TestClient):
    first = _must_competitor(client, CHAN_A, "@DupeHandle")
    second = _must_competitor(client, CHAN_A, "@dupehandle")
    assert second["id"] == first["id"]


def test_get_unknown_competitor_404(client: TestClient):
    resp = client.get(f"/api/v1/channels/{CHAN_A}/competitors/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "competitor_not_found"


def test_archive_competitor_flow(client: TestClient):
    comp = _must_competitor(client, CHAN_A, "@ArchiveMe")
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/archive"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "archived"

    default_list = client.get(f"/api/v1/channels/{CHAN_A}/competitors")
    assert comp["id"] not in [i["id"] for i in default_list.json()["items"]]
    with_archived = client.get(
        f"/api/v1/channels/{CHAN_A}/competitors",
        params={"include_archived": True},
    )
    assert comp["id"] in [i["id"] for i in with_archived.json()["items"]]


# ---------------------------------------------------------------------------
# Cross-channel isolation: every scoped endpoint 404s (never 403)
# ---------------------------------------------------------------------------


def test_cross_channel_competitor_endpoints_404(
    client: TestClient, tmp_path: Path
):
    comp = _must_competitor(client, CHAN_A, "@IsolatedRival")
    cid = comp["id"]
    mp4 = _media_bytes("mp4", tmp_path)

    checks = [
        client.get(f"/api/v1/channels/{CHAN_B}/competitors/{cid}"),
        client.post(f"/api/v1/channels/{CHAN_B}/competitors/{cid}/archive"),
        client.get(f"/api/v1/channels/{CHAN_B}/competitors/{cid}/videos"),
        client.post(
            f"/api/v1/channels/{CHAN_B}/competitors/{cid}/videos",
            json={"source": "pasted_metadata", "title": "x"},
        ),
        client.post(
            f"/api/v1/channels/{CHAN_B}/competitors/{cid}/metadata/fetch"
        ),
        client.post(
            f"/api/v1/channels/{CHAN_B}/competitors/{cid}/videos/attach",
            files={"video": ("clip.mp4", mp4, "video/mp4")},
        ),
    ]
    for resp in checks:
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] in (
            "competitor_not_found",
            "channel_not_found",
        )
    # And B's own competitor list stays empty of A's competitor.
    listing = client.get(f"/api/v1/channels/{CHAN_B}/competitors")
    assert all(i["id"] != cid for i in listing.json()["items"])
    # A's competitor is untouched (archive attempt did not land).
    got = client.get(f"/api/v1/channels/{CHAN_A}/competitors/{cid}")
    assert got.json()["status"] == "active"


# ---------------------------------------------------------------------------
# Video attach: metadata paths (no fetch involved)
# ---------------------------------------------------------------------------


def test_attach_pasted_metadata_video(client: TestClient):
    comp = _must_competitor(client, CHAN_A, "@PasteRival")
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/videos",
        json={
            "source": "pasted_metadata",
            "title": "Rival Video Title",
            "description": "pasted description",
            "tags": ["facts", "viral"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "pasted_metadata"
    assert body["title"] == "Rival Video Title"
    assert body["tags"] == ["facts", "viral"]
    assert body["analysis_status"] == "not_queued"
    assert body["competitor_channel_id"] == comp["id"]

    listing = client.get(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/videos"
    )
    assert [v["id"] for v in listing.json()["items"]] == [body["id"]]


def test_attach_url_metadata_extracts_video_id(client: TestClient):
    comp = _must_competitor(client, CHAN_A, "@UrlRival")
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/videos",
        json={
            "source": "url_metadata",
            "video_url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "url_metadata"
    assert body["external_video_id"] == VIDEO_ID
    assert body["video_url"].endswith(VIDEO_ID)


def test_attach_metadata_rejects_file_source_and_empty(client: TestClient):
    comp = _must_competitor(client, CHAN_A, "@StrictRival")
    bad_source = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/videos",
        json={"source": "attached_file", "title": "nope"},
    )
    assert bad_source.status_code == 422
    empty = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/videos",
        json={"source": "pasted_metadata"},
    )
    assert empty.status_code == 422


# ---------------------------------------------------------------------------
# Video attach: real file upload (streaming multipart)
# ---------------------------------------------------------------------------


def test_attach_real_mp4_probes_and_stores(client: TestClient, tmp_path: Path):
    from app.config import get_settings

    comp = _must_competitor(client, CHAN_A, "@FileRival")
    mp4 = _media_bytes("mp4", tmp_path)
    jpg = _media_bytes("jpg", tmp_path)
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/videos/attach",
        files={
            "video": ("clip.mp4", mp4, "video/mp4"),
            "thumbnail": ("thumb.jpg", jpg, "image/jpeg"),
        },
        data={"title": "Rival Clip"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "attached_file"
    assert body["title"] == "Rival Clip"
    assert body["duration_s"] == pytest.approx(1.0, abs=0.35)
    assert body["width"] == 64 and body["height"] == 64
    assert body["mime_type"] == "video/mp4"
    assert body["file_size_bytes"] == len(mp4)
    assert len(body["file_sha256"]) == 64
    assert body["storage_path"]
    stored = get_settings().data_dir / body["storage_path"]
    assert stored.exists() and stored.stat().st_size == len(mp4)
    assert body["thumbnail_storage_path"]
    assert len(body["thumbnail_sha256"]) == 64
    thumb = get_settings().data_dir / body["thumbnail_storage_path"]
    assert thumb.exists()


def test_attach_invalid_file_422_machine_readable_no_row(
    client: TestClient,
):
    comp = _must_competitor(client, CHAN_A, "@BadFileRival")
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/videos/attach",
        files={"video": ("clip.mp4", b"this is not a video", "video/mp4")},
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"]  # machine-readable code present
    assert error["details"]["reason"] in (
        "ffprobe_failed",
        "no_video_stream",
        "zero_duration",
        "unsupported_format",
    )
    listing = client.get(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/videos"
    )
    assert listing.json()["items"] == []  # failed attach left no row


def test_attach_extension_not_trusted_content_wins(
    client: TestClient, tmp_path: Path
):
    # The media module validates by PROBED content, never by filename
    # extension alone: real mp4 bytes named ".txt" are stored (under a
    # canonical .mp4 storage name), while fake bytes named ".mp4" are
    # rejected (see test_attach_invalid_file_422_machine_readable_no_row).
    comp = _must_competitor(client, CHAN_A, "@ExtRival")
    mp4 = _media_bytes("mp4", tmp_path)
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/videos/attach",
        files={"video": ("notes.txt", mp4, "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["storage_path"].endswith(".mp4")
    assert resp.json()["mime_type"] == "video/mp4"


# ---------------------------------------------------------------------------
# Metadata fetch (boundary patched — no live HTTP)
# ---------------------------------------------------------------------------


def test_metadata_fetch_unavailable_without_credential(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    import app.style_lab.metadata as metadata_module
    from app.style_lab.metadata import ChannelMetadata

    async def fake_fetch(**kwargs):
        return ChannelMetadata(
            status="metadata_unavailable",
            reason="no_youtube_credential",
            detail="No usable YouTube credential is configured.",
            external_channel_id=kwargs.get("channel_id"),
            handle=kwargs.get("handle"),
        )

    monkeypatch.setattr(
        metadata_module, "fetch_channel_metadata", fake_fetch
    )
    comp = _must_competitor(client, CHAN_A, "@NoCredRival")
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/metadata/fetch"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metadata_status"] == "metadata_unavailable"
    assert body["fetch_status"] == "metadata_unavailable"
    # Persisted, not just reported.
    got = client.get(f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}")
    assert got.json()["metadata_status"] == "metadata_unavailable"


def test_metadata_fetch_fetched_fills_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    import app.style_lab.metadata as metadata_module
    from app.style_lab.metadata import ChannelMetadata

    seen: dict = {}

    async def fake_fetch(**kwargs):
        seen.update(kwargs)
        return ChannelMetadata(
            status="fetched",
            external_channel_id=RIVAL_CHANNEL_ID,
            display_name="Rival Channel",
            handle="FetchRival",
            channel_url=f"https://www.youtube.com/channel/{RIVAL_CHANNEL_ID}",
            subscriber_count=12345,
            raw_json={"channels": {"items": [{"id": RIVAL_CHANNEL_ID}]}},
        )

    monkeypatch.setattr(
        metadata_module, "fetch_channel_metadata", fake_fetch
    )
    comp = _must_competitor(client, CHAN_A, "@FetchRival")
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/metadata/fetch"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metadata_status"] == "fetched"
    assert body["display_name"] == "Rival Channel"
    assert body["external_channel_id"] == RIVAL_CHANNEL_ID
    assert body["metadata"]["subscriber_count"] == 12345
    assert seen["handle"] == "FetchRival"


# ---------------------------------------------------------------------------
# Analysis seam
# ---------------------------------------------------------------------------


def test_request_analysis_flips_status_to_queued(client: TestClient):
    comp = _must_competitor(client, CHAN_A, "@AnalyseRival")
    video = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{comp['id']}/videos",
        json={"source": "pasted_metadata", "title": "Analyse me"},
    ).json()
    assert video["analysis_status"] == "not_queued"

    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitor-videos/{video['id']}/analysis"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analysis_status"] == "queued"
    assert body["analysis_requested_at"] is not None

    cross = client.post(
        f"/api/v1/channels/{CHAN_B}/competitor-videos/{video['id']}/analysis"
    )
    assert cross.status_code == 404

    unknown = client.post(
        f"/api/v1/channels/{CHAN_A}/competitor-videos/nope/analysis"
    )
    assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# Style profiles
# ---------------------------------------------------------------------------


def test_profile_create_list_and_activate_supersedes(client: TestClient):
    comp = _must_competitor(client, CHAN_A, "@ProfileRival")
    first = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles",
        json={"name": "Rival Style v1", "competitor_ids": [comp["id"]]},
    )
    assert first.status_code == 201, first.text
    p1 = first.json()
    assert p1["status"] == "draft"
    assert p1["channel_id"] == CHAN_A
    assert p1["competitor_ids"] == [comp["id"]]

    second = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles",
        json={"name": "Rival Style v2", "competitor_ids": [comp["id"]]},
    )
    assert second.status_code == 201
    p2 = second.json()

    act1 = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles/{p1['id']}/activate"
    )
    assert act1.status_code == 200
    assert act1.json()["status"] == "active"

    act2 = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles/{p2['id']}/activate"
    )
    assert act2.status_code == 200
    assert act2.json()["status"] == "active"

    listing = client.get(f"/api/v1/channels/{CHAN_A}/style-profiles")
    by_id = {p["id"]: p for p in listing.json()["items"]}
    assert by_id[p1["id"]]["status"] == "superseded"
    assert by_id[p2["id"]]["status"] == "active"

    # Scoped listing: channel B sees none of A's profiles.
    other = client.get(f"/api/v1/channels/{CHAN_B}/style-profiles")
    assert other.json()["items"] == []
    # Cross-channel activate is a 404, not a leak.
    cross = client.post(
        f"/api/v1/channels/{CHAN_B}/style-profiles/{p2['id']}/activate"
    )
    assert cross.status_code == 404


def test_profile_create_with_other_channels_competitor_is_4xx(
    client: TestClient,
):
    comp_b = _must_competitor(client, CHAN_B, "@OtherOwnersRival")
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles",
        json={"name": "Stolen Style", "competitor_ids": [comp_b["id"]]},
    )
    assert 400 <= resp.status_code < 500, resp.text
    assert resp.json()["error"]["code"] == "competitor_not_in_channel"

    unknown = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles",
        json={"name": "Ghost Style", "competitor_ids": ["missing-id"]},
    )
    assert 400 <= unknown.status_code < 500
