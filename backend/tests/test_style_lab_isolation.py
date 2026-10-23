"""Style Lab cross-channel isolation — the dedicated adversarial pass.

Sibling suites already cover the happy paths and a first cross-channel
matrix (``tests/test_style_lab_api.py``, ``tests/test_style_lab_store.py``).
This file is the adversarial complement, in the spirit of
``tests/test_channel_isolation.py``: two owner channels A and B, with
EVERYTHING created under A — a competitor, an attached-file video row,
a pasted-metadata video and a style profile — and then every way B
could try to see or use A's rows is exercised:

API level (real HTTP through the sidecar app):

* list competitors / list style profiles for B never contain A's ids;
* get / archive / metadata-fetch / list-videos / attach-metadata /
  attach-file on A's competitor under B's channel -> 404, never data,
  never 403, and A's rows are byte-for-byte untouched afterwards;
* queueing analysis on A's video under B -> 404 and the video stays
  ``not_queued``;
* activating A's profile under B -> 404 and it stays ``draft``;
* creating a profile under B that references A's competitor id fails
  (4xx, no profile created);
* the same attacks addressed via B's *YouTube* channel id in the path
  (the service also resolves that form) still 404;
* positive control: B CAN create its own competitor with the same
  handle — dedupe/scope is per-channel, so it is a distinct row.

Store level (``StyleLabStore`` over an isolated ``make_db`` world):

* scoped reads return ``None`` / ``[]`` across channels;
* scoped writes raise :class:`StyleLabScopeError`;
* ``create_profile`` under B referencing A's competitor raises
  :class:`StyleLabScopeError`; an unknown competitor id raises
  :class:`StyleLabNotFoundError`;
* after every cross-channel attempt, A's rows are unchanged.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SUFFIX = uuid.uuid4().hex[:10]
CHAN_A = f"sl-iso-a-{_SUFFIX}"
CHAN_B = f"sl-iso-b-{_SUFFIX}"
YT_A = f"UCisoA{_SUFFIX}xxxx"
YT_B = f"UCisoB{_SUFFIX}xxxx"
HANDLE = f"IsoRival{_SUFFIX[:8]}"


# ---------------------------------------------------------------------------
# API level
# ---------------------------------------------------------------------------


def _seed_channels() -> None:
    from app import db as db_module

    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        for chan_id, yt_id, title in (
            (CHAN_A, YT_A, "Isolation Owner A"),
            (CHAN_B, YT_B, "Isolation Owner B"),
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


def _tiny_mp4(tmp_path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not available for media fixtures")
    out = tmp_path / "iso-clip.mp4"
    proc = subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i",
         "testsrc=duration=1:size=64x64:rate=10", "-pix_fmt", "yuv420p",
         str(out)],
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-500:]
    return out.read_bytes()


@pytest.fixture()
def world_a(client: TestClient, tmp_path: Path) -> dict:
    """Everything created under channel A; nothing under B.

    The handle is unique per invocation: the backend DB is shared
    across the whole test session and the store dedupes competitors
    per (channel, handle), so reusing one handle would pile every
    test's videos/profiles onto a single competitor row.
    """
    handle = f"IsoR{uuid.uuid4().hex[:12]}"
    comp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors",
        json={"input": f"@{handle}"},
    )
    assert comp.status_code == 201, comp.text
    competitor = comp.json()

    mp4 = _tiny_mp4(tmp_path)
    attached = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{competitor['id']}"
        "/videos/attach",
        files={"video": ("clip.mp4", mp4, "video/mp4")},
        data={"title": "A attached video"},
    )
    assert attached.status_code == 201, attached.text

    pasted = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{competitor['id']}/videos",
        json={"source": "pasted_metadata", "title": "A pasted video",
              "tags": ["iso"]},
    )
    assert pasted.status_code == 201, pasted.text

    profile = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles",
        json={"name": "A style", "competitor_ids": [competitor["id"]]},
    )
    assert profile.status_code == 201, profile.text

    return {
        "competitor": competitor,
        "handle": handle,
        "attached": attached.json(),
        "pasted": pasted.json(),
        "profile": profile.json(),
        "mp4_sha256": hashlib.sha256(mp4).hexdigest(),
    }


def _assert_404_no_leak(resp, forbidden_fragments: list[str]) -> None:
    assert resp.status_code == 404, resp.text
    assert resp.status_code != 403
    body = resp.text
    for fragment in forbidden_fragments:
        assert fragment not in body


def test_b_lists_never_contain_a_rows(client: TestClient, world_a: dict):
    comp_id = world_a["competitor"]["id"]
    profile_id = world_a["profile"]["id"]

    # B's lists may legitimately hold B's own rows from other tests in
    # this file (shared session DB) — the isolation invariant is that
    # none of them is A's, and every row shown is scoped to B.
    competitors_b = client.get(f"/api/v1/channels/{CHAN_B}/competitors")
    assert competitors_b.status_code == 200
    b_comp_ids = [i["id"] for i in competitors_b.json()["items"]]
    assert comp_id not in b_comp_ids
    assert all(
        i["channel_id"] == CHAN_B for i in competitors_b.json()["items"])

    profiles_b = client.get(f"/api/v1/channels/{CHAN_B}/style-profiles")
    assert profiles_b.status_code == 200
    b_profile_ids = [i["id"] for i in profiles_b.json()["items"]]
    assert profile_id not in b_profile_ids
    assert all(
        i["channel_id"] == CHAN_B for i in profiles_b.json()["items"])

    # Sanity: A sees its own rows.
    competitors_a = client.get(f"/api/v1/channels/{CHAN_A}/competitors")
    assert comp_id in [i["id"] for i in competitors_a.json()["items"]]
    profiles_a = client.get(f"/api/v1/channels/{CHAN_A}/style-profiles")
    assert profile_id in [i["id"] for i in profiles_a.json()["items"]]


def test_b_cannot_get_archive_or_fetch_a_competitor(
    client: TestClient, world_a: dict
):
    cid = world_a["competitor"]["id"]
    # NOTE: the caller-supplied id itself is echoed in the 404 error
    # message ("No competitor with id '…'") — that is not a leak, the
    # caller already knew it.  What must never leak is A's DATA:
    # the handle and the video titles.
    secrets = [world_a["handle"], "A attached video", "A pasted video"]

    _assert_404_no_leak(
        client.get(f"/api/v1/channels/{CHAN_B}/competitors/{cid}"), secrets)
    _assert_404_no_leak(
        client.post(f"/api/v1/channels/{CHAN_B}/competitors/{cid}/archive"),
        secrets)
    _assert_404_no_leak(
        client.post(
            f"/api/v1/channels/{CHAN_B}/competitors/{cid}/metadata/fetch"),
        secrets)

    # A's competitor is untouched: still active, metadata never fetched.
    got = client.get(f"/api/v1/channels/{CHAN_A}/competitors/{cid}")
    assert got.status_code == 200
    assert got.json()["status"] == "active"
    assert got.json()["metadata_status"] == "not_fetched"


def test_b_cannot_list_or_attach_videos_on_a_competitor(
    client: TestClient, world_a: dict, tmp_path: Path
):
    cid = world_a["competitor"]["id"]
    video_ids = [world_a["attached"]["id"], world_a["pasted"]["id"]]

    _assert_404_no_leak(
        client.get(f"/api/v1/channels/{CHAN_B}/competitors/{cid}/videos"),
        ["A attached video", "A pasted video"])

    _assert_404_no_leak(
        client.post(
            f"/api/v1/channels/{CHAN_B}/competitors/{cid}/videos",
            json={"source": "pasted_metadata", "title": "B intrusion"},
        ),
        ["A attached video", "A pasted video"])

    mp4 = _tiny_mp4(tmp_path)
    _assert_404_no_leak(
        client.post(
            f"/api/v1/channels/{CHAN_B}/competitors/{cid}/videos/attach",
            files={"video": ("clip.mp4", mp4, "video/mp4")},
        ),
        ["A attached video", "A pasted video"])

    # A's video list is exactly the two original rows, in order.
    listing = client.get(f"/api/v1/channels/{CHAN_A}/competitors/{cid}/videos")
    assert [v["id"] for v in listing.json()["items"]] == video_ids
    assert all(
        v["analysis_status"] == "not_queued"
        for v in listing.json()["items"]
    )


def test_b_cannot_queue_analysis_on_a_video(client: TestClient, world_a: dict):
    for video in (world_a["attached"], world_a["pasted"]):
        resp = client.post(
            f"/api/v1/channels/{CHAN_B}/competitor-videos/{video['id']}"
            "/analysis"
        )
        _assert_404_no_leak(resp, [video["title"] or "~~none~~"])

    listing = client.get(
        f"/api/v1/channels/{CHAN_A}/competitors"
        f"/{world_a['competitor']['id']}/videos"
    )
    assert all(
        v["analysis_status"] == "not_queued"
        and v["analysis_requested_at"] is None
        for v in listing.json()["items"]
    )


def test_b_cannot_activate_a_profile(client: TestClient, world_a: dict):
    pid = world_a["profile"]["id"]
    resp = client.post(
        f"/api/v1/channels/{CHAN_B}/style-profiles/{pid}/activate"
    )
    _assert_404_no_leak(resp, ["A style"])

    listing = client.get(f"/api/v1/channels/{CHAN_A}/style-profiles")
    by_id = {p["id"]: p for p in listing.json()["items"]}
    assert by_id[pid]["status"] == "draft"  # activation did not land


def test_b_profile_referencing_a_competitor_fails(
    client: TestClient, world_a: dict
):
    cid = world_a["competitor"]["id"]
    resp = client.post(
        f"/api/v1/channels/{CHAN_B}/style-profiles",
        json={"name": "B stolen style", "competitor_ids": [cid]},
    )
    assert 400 <= resp.status_code < 500, resp.text
    assert resp.json()["error"]["code"] == "competitor_not_in_channel"

    # Mixed list (own + foreign) is refused as a whole, too.
    own = client.post(
        f"/api/v1/channels/{CHAN_B}/competitors",
        json={"input": f"@IsoOwn{_SUFFIX[:8]}"},
    )
    assert own.status_code == 201, own.text
    mixed = client.post(
        f"/api/v1/channels/{CHAN_B}/style-profiles",
        json={"name": "B mixed style",
              "competitor_ids": [own.json()["id"], cid]},
    )
    assert 400 <= mixed.status_code < 500, mixed.text

    # No profile was created under B by either attempt.
    profiles_b = client.get(f"/api/v1/channels/{CHAN_B}/style-profiles")
    assert profiles_b.json()["items"] == [] or all(
        p["name"] not in ("B stolen style", "B mixed style")
        for p in profiles_b.json()["items"]
    )


def test_b_attacks_via_youtube_channel_id_still_404(
    client: TestClient, world_a: dict
):
    """The service also resolves a channel's YouTube id in the path;
    that alias must not widen B's reach over A's rows."""
    cid = world_a["competitor"]["id"]
    pid = world_a["profile"]["id"]
    vid = world_a["attached"]["id"]
    checks = [
        client.get(f"/api/v1/channels/{YT_B}/competitors/{cid}"),
        client.post(f"/api/v1/channels/{YT_B}/competitors/{cid}/archive"),
        client.get(f"/api/v1/channels/{YT_B}/competitors/{cid}/videos"),
        client.post(f"/api/v1/channels/{YT_B}/competitor-videos/{vid}"
                    "/analysis"),
        client.post(f"/api/v1/channels/{YT_B}/style-profiles/{pid}"
                    "/activate"),
    ]
    for resp in checks:
        assert resp.status_code == 404, resp.text


def test_same_handle_under_b_is_a_distinct_row(client: TestClient):
    """Positive control: scoping is per-channel, not a global lock —
    B adding the same competitor handle succeeds as B's own row."""
    a_resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors",
        json={"input": f"@{HANDLE}"},
    )
    assert a_resp.status_code == 201, a_resp.text
    a_comp = a_resp.json()

    resp = client.post(
        f"/api/v1/channels/{CHAN_B}/competitors",
        json={"input": f"@{HANDLE}"},
    )
    assert resp.status_code == 201, resp.text
    b_comp = resp.json()
    assert b_comp["id"] != a_comp["id"]
    assert b_comp["channel_id"] == CHAN_B

    a_list = client.get(f"/api/v1/channels/{CHAN_A}/competitors")
    a_ids = [i["id"] for i in a_list.json()["items"]]
    assert a_comp["id"] in a_ids and b_comp["id"] not in a_ids
    b_list = client.get(f"/api/v1/channels/{CHAN_B}/competitors")
    b_ids = [i["id"] for i in b_list.json()["items"]]
    assert b_comp["id"] in b_ids and a_comp["id"] not in b_ids


def test_unknown_owner_channel_gets_404_everywhere(
    client: TestClient, world_a: dict
):
    ghost = "sl-iso-ghost-channel"
    cid = world_a["competitor"]["id"]
    checks = [
        client.get(f"/api/v1/channels/{ghost}/competitors"),
        client.get(f"/api/v1/channels/{ghost}/competitors/{cid}"),
        client.get(f"/api/v1/channels/{ghost}/style-profiles"),
        client.post(
            f"/api/v1/channels/{ghost}/competitors",
            json={"input": f"@{HANDLE}"}),
    ]
    for resp in checks:
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "channel_not_found"


# ---------------------------------------------------------------------------
# Store level (isolated DB world)
# ---------------------------------------------------------------------------


@pytest.fixture()
def store_world(tmp_path):
    from app.style_lab.store import StyleLabStore
    from tests.execution_fixtures import make_db

    _db_path, factory = make_db(tmp_path)
    store = StyleLabStore(factory)
    now = int(time.time() * 1000)
    channels = {}
    conn = factory()
    try:
        for label in ("A", "B"):
            channel_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO channels (id, youtube_channel_id, title,"
                " language, status, created_at, updated_at) VALUES"
                " (?, ?, ?, 'en-US', 'active', ?, ?)",
                (channel_id, f"UC-store-{label}-{uuid.uuid4().hex[:8]}",
                 f"Store Owner {label}", now, now),
            )
            channels[label] = channel_id
        conn.commit()
    finally:
        conn.close()
    return {"store": store, "factory": factory, "channels": channels}


@pytest.fixture()
def store_a(store_world):
    from app.style_lab.parsing import parse_competitor_input

    store = store_world["store"]
    a = store_world["channels"]["A"]
    competitor = store.create_competitor(
        a, parse_competitor_input("@StoreRival"))
    attached = store.add_video(
        competitor["id"],
        {"source": "attached_file", "title": "store attached",
         "storage_path": "style_lab/x/clip.mp4",
         "file_sha256": "0" * 64},
        channel_id=a,
    )
    pasted = store.add_video(
        competitor["id"],
        {"source": "pasted_metadata", "title": "store pasted"},
        channel_id=a,
    )
    profile = store.create_profile(a, "store profile", [competitor["id"]])
    return {"competitor": competitor, "attached": attached,
            "pasted": pasted, "profile": profile}


def test_store_scoped_reads_hide_a_from_b(store_world, store_a):
    store = store_world["store"]
    b = store_world["channels"]["B"]
    comp = store_a["competitor"]
    profile = store_a["profile"]

    assert store.list_competitors(b) == []
    assert store.get_competitor(comp["id"], channel_id=b) is None
    assert store.list_videos(comp["id"], channel_id=b) == []
    assert store.get_video(store_a["attached"]["id"], channel_id=b) is None
    assert store.get_video(store_a["pasted"]["id"], channel_id=b) is None
    assert store.list_profiles(b) == []
    assert store.get_profile(profile["id"], channel_id=b) is None


def test_store_scoped_writes_raise_scope_error(store_world, store_a):
    from app.style_lab.store import StyleLabScopeError

    store = store_world["store"]
    b = store_world["channels"]["B"]
    comp = store_a["competitor"]

    with pytest.raises(StyleLabScopeError):
        store.archive_competitor(comp["id"], channel_id=b)
    with pytest.raises(StyleLabScopeError):
        store.add_video(
            comp["id"],
            {"source": "pasted_metadata", "title": "B intrusion"},
            channel_id=b,
        )
    with pytest.raises(StyleLabScopeError):
        store.set_analysis_status(
            store_a["attached"]["id"], "queued", channel_id=b)
    with pytest.raises(StyleLabScopeError):
        store.activate_profile(store_a["profile"]["id"], channel_id=b)


def test_store_create_profile_scope_and_not_found(store_world, store_a):
    from app.style_lab.store import (
        StyleLabNotFoundError,
        StyleLabScopeError,
    )

    store = store_world["store"]
    b = store_world["channels"]["B"]

    with pytest.raises(StyleLabScopeError):
        store.create_profile(b, "B theft", [store_a["competitor"]["id"]])
    with pytest.raises(StyleLabNotFoundError):
        store.create_profile(b, "B ghost", ["no-such-competitor-id"])
    assert store.list_profiles(b) == []


def test_store_a_rows_unchanged_after_cross_attempts(store_world, store_a):
    from app.style_lab.store import StyleLabScopeError

    store = store_world["store"]
    a = store_world["channels"]["A"]
    b = store_world["channels"]["B"]
    comp = store_a["competitor"]

    for attempt in (
        lambda: store.archive_competitor(comp["id"], channel_id=b),
        lambda: store.set_analysis_status(
            store_a["attached"]["id"], "queued", channel_id=b),
        lambda: store.activate_profile(
            store_a["profile"]["id"], channel_id=b),
        lambda: store.create_profile(b, "B theft", [comp["id"]]),
    ):
        with pytest.raises(StyleLabScopeError):
            attempt()

    fresh_comp = store.get_competitor(comp["id"], channel_id=a)
    assert fresh_comp["status"] == "active"
    videos = store.list_videos(comp["id"], channel_id=a)
    assert [v["id"] for v in videos] == [
        store_a["attached"]["id"], store_a["pasted"]["id"]]
    assert all(v["analysis_status"] == "not_queued" for v in videos)
    fresh_profile = store.get_profile(store_a["profile"]["id"], channel_id=a)
    assert fresh_profile["status"] == "draft"
