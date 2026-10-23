"""Style Lab store: CRUD, dedupe, owner-channel isolation, profiles.

Two owner channels (A and B) are seeded in the real section-15 schema;
every isolation assertion checks that channel B cannot list, read or
attach to channel A's competitors / videos / profiles.
"""

from __future__ import annotations

import time
import uuid

import pytest

from app.style_lab.parsing import ParsedCompetitor, parse_competitor_input
from app.style_lab.store import (
    StyleLabNotFoundError,
    StyleLabScopeError,
    StyleLabStore,
)
from tests.execution_fixtures import make_db

CHANNEL_ID_EXT = "UC" + "a" * 22
VIDEO_ID_EXT = "dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# Fixtures / seeding
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
            (channel_id, f"UC-owner-{label}-{uuid.uuid4().hex[:8]}",
             f"Owner {label}", ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return channel_id


@pytest.fixture()
def env(tmp_path):
    _db_path, factory = make_db(tmp_path)
    return {
        "factory": factory,
        "store": StyleLabStore(factory),
        "channel_a": _seed_channel(factory, "A"),
        "channel_b": _seed_channel(factory, "B"),
    }


def _competitor(env, channel=None, raw="https://www.youtube.com/@CoolChannel"):
    return env["store"].create_competitor(
        channel or env["channel_a"], parse_competitor_input(raw)
    )


# ---------------------------------------------------------------------------
# Competitor CRUD
# ---------------------------------------------------------------------------


def test_create_and_get_competitor(env):
    store = env["store"]
    row = _competitor(env)
    assert row["channel_id"] == env["channel_a"]
    assert row["platform"] == "youtube"
    assert row["source_kind"] == "channel_url"
    assert row["handle"] == "CoolChannel"
    assert row["channel_url"] == "https://www.youtube.com/@CoolChannel"
    assert row["source_input"] == "https://www.youtube.com/@CoolChannel"
    assert row["metadata_status"] == "not_fetched"
    assert row["status"] == "active"
    assert row["created_at"] > 0 and row["updated_at"] >= row["created_at"]

    fetched = store.get_competitor(row["id"])
    assert fetched is not None and fetched["id"] == row["id"]
    assert store.get_competitor("does-not-exist") is None


def test_create_competitor_from_channel_url_and_bare_id(env):
    store = env["store"]
    by_url = store.create_competitor(
        env["channel_a"],
        parse_competitor_input(f"https://www.youtube.com/channel/{CHANNEL_ID_EXT}"),
        display_name="Facts Channel",
    )
    assert by_url["external_channel_id"] == CHANNEL_ID_EXT
    assert by_url["display_name"] == "Facts Channel"
    assert by_url["source_kind"] == "channel_url"


def test_create_competitor_from_video_url_keeps_channel_unknown(env):
    # A video URL alone cannot identify the channel: the competitor row
    # is created with channel identifiers NULL (metadata fetch resolves).
    store = env["store"]
    parsed = parse_competitor_input(f"https://youtu.be/{VIDEO_ID_EXT}")
    row = store.create_competitor(env["channel_a"], parsed)
    assert row["source_kind"] == "video_url"
    assert row["external_channel_id"] is None
    assert row["handle"] is None


def test_list_competitors_scoped_and_archived_filter(env):
    store = env["store"]
    a1 = _competitor(env, raw="@AlphaOne")
    a2 = _competitor(env, raw="@BetaTwo")
    _competitor(env, channel=env["channel_b"], raw="@AlphaOne")

    mine = store.list_competitors(env["channel_a"])
    assert [r["id"] for r in mine] == [a1["id"], a2["id"]]
    assert len(store.list_competitors(env["channel_b"])) == 1

    store.archive_competitor(a1["id"])
    assert [r["id"] for r in store.list_competitors(env["channel_a"])] == [a2["id"]]
    with_archived = store.list_competitors(
        env["channel_a"], include_archived=True
    )
    assert {r["id"] for r in with_archived} == {a1["id"], a2["id"]}


def test_archive_competitor(env):
    store = env["store"]
    row = _competitor(env)
    archived = store.archive_competitor(row["id"])
    assert archived is not None and archived["status"] == "archived"
    assert store.archive_competitor("does-not-exist") is None


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


def test_dedupe_by_external_channel_id(env):
    store = env["store"]
    first = store.create_competitor(
        env["channel_a"],
        parse_competitor_input(f"https://www.youtube.com/channel/{CHANNEL_ID_EXT}"),
    )
    second = store.create_competitor(
        env["channel_a"], parse_competitor_input(CHANNEL_ID_EXT)
    )
    assert second["id"] == first["id"]
    assert len(store.list_competitors(env["channel_a"])) == 1


def test_dedupe_by_handle_case_insensitive(env):
    store = env["store"]
    first = _competitor(env, raw="@CoolChannel")
    second = store.create_competitor(
        env["channel_a"],
        parse_competitor_input("https://www.youtube.com/@coolchannel"),
    )
    assert second["id"] == first["id"]
    assert len(store.list_competitors(env["channel_a"])) == 1


def test_dedupe_backfills_missing_external_id(env):
    store = env["store"]
    first = _competitor(env, raw="@CoolChannel")
    assert first["external_channel_id"] is None
    merged = ParsedCompetitor(
        source_kind="channel_url",
        external_channel_id=CHANNEL_ID_EXT,
        handle="CoolChannel",
        channel_url="https://www.youtube.com/@CoolChannel",
        source_input="https://www.youtube.com/@CoolChannel",
    )
    second = store.create_competitor(env["channel_a"], merged)
    assert second["id"] == first["id"]
    assert second["external_channel_id"] == CHANNEL_ID_EXT


def test_dedupe_does_not_cross_channels(env):
    store = env["store"]
    a = _competitor(env, raw="@CoolChannel")
    b = _competitor(env, channel=env["channel_b"], raw="@CoolChannel")
    assert a["id"] != b["id"]


def test_archived_competitor_does_not_dedupe(env):
    store = env["store"]
    first = _competitor(env, raw="@CoolChannel")
    store.archive_competitor(first["id"])
    second = _competitor(env, raw="@CoolChannel")
    assert second["id"] != first["id"]
    assert second["status"] == "active"


# ---------------------------------------------------------------------------
# Cross-channel isolation: competitors
# ---------------------------------------------------------------------------


def test_cross_channel_competitor_reads_and_writes(env):
    store = env["store"]
    a_row = _competitor(env)

    # Read with the wrong owner scope looks like "not found".
    assert store.get_competitor(a_row["id"], channel_id=env["channel_b"]) is None
    assert store.get_competitor(a_row["id"], channel_id=env["channel_a"]) is not None
    # Lists never cross.
    assert store.list_competitors(env["channel_b"]) == []
    # Cross-channel archive is refused and changes nothing.
    with pytest.raises(StyleLabScopeError):
        store.archive_competitor(a_row["id"], channel_id=env["channel_b"])
    assert store.get_competitor(a_row["id"])["status"] == "active"


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------


def _video(env, competitor_id, **overrides):
    fields = {
        "source": "attached_file",
        "storage_path": "/data/style-lab/v1.mp4",
        "file_sha256": "ab" * 32,
        "file_size_bytes": 123456,
        "mime_type": "video/mp4",
        "duration_s": 95.5,
        "width": 1920,
        "height": 1080,
        "title": "A competitor video",
    }
    fields.update(overrides)
    return env["store"].add_video(competitor_id, fields)


def test_add_video_defaults_and_fields(env):
    store = env["store"]
    comp = _competitor(env)
    video = _video(env, comp["id"], tags=["facts", "urdu"])
    assert video["competitor_channel_id"] == comp["id"]
    assert video["analysis_status"] == "not_queued"
    assert video["analysis_requested_at"] is None
    assert video["tags"] == ["facts", "urdu"]
    assert video["duration_s"] == 95.5

    fetched = store.get_video(video["id"])
    assert fetched is not None and fetched["title"] == "A competitor video"
    assert [v["id"] for v in store.list_videos(comp["id"])] == [video["id"]]


def test_add_video_kwargs_form_and_url_metadata(env):
    store = env["store"]
    comp = _competitor(env)
    video = store.add_video(
        comp["id"],
        source="url_metadata",
        external_video_id=VIDEO_ID_EXT,
        video_url=f"https://www.youtube.com/watch?v={VIDEO_ID_EXT}",
        title="From URL",
        description="desc",
    )
    assert video["source"] == "url_metadata"
    assert video["external_video_id"] == VIDEO_ID_EXT


def test_add_video_validation(env):
    store = env["store"]
    comp = _competitor(env)
    with pytest.raises(ValueError):
        store.add_video(comp["id"], {"source": "carrier_pigeon"})
    with pytest.raises(StyleLabNotFoundError):
        store.add_video("does-not-exist", {"source": "attached_file"})


def test_cross_channel_video_isolation(env):
    store = env["store"]
    comp_a = _competitor(env)
    video = _video(env, comp_a["id"])

    # Channel B cannot attach to channel A's competitor.
    with pytest.raises(StyleLabScopeError):
        store.add_video(
            comp_a["id"], {"source": "pasted_metadata", "title": "x"},
            channel_id=env["channel_b"],
        )
    # ... nor read the video or list it through the competitor.
    assert store.get_video(video["id"], channel_id=env["channel_b"]) is None
    assert store.list_videos(comp_a["id"], channel_id=env["channel_b"]) == []
    # Channel A can.
    assert store.get_video(video["id"], channel_id=env["channel_a"]) is not None
    assert len(store.list_videos(comp_a["id"], channel_id=env["channel_a"])) == 1


# ---------------------------------------------------------------------------
# Analysis status transitions
# ---------------------------------------------------------------------------


def test_analysis_status_transitions_stamp_request_time(env):
    store = env["store"]
    comp = _competitor(env)
    video = _video(env, comp["id"])
    assert video["analysis_requested_at"] is None

    before = int(time.time() * 1000)
    queued = store.set_analysis_status(video["id"], "queued")
    assert queued["analysis_status"] == "queued"
    assert queued["analysis_requested_at"] is not None
    assert queued["analysis_requested_at"] >= before
    stamp = queued["analysis_requested_at"]

    # Later transitions keep the original request stamp.
    analysing = store.set_analysis_status(video["id"], "analysing")
    assert analysing["analysis_requested_at"] == stamp
    done = store.set_analysis_status(video["id"], "analysed")
    assert done["analysis_status"] == "analysed"
    assert done["analysis_requested_at"] == stamp

    failed = store.set_analysis_status(video["id"], "analysis_failed")
    assert failed["analysis_status"] == "analysis_failed"


def test_analysis_status_validation_and_scope(env):
    store = env["store"]
    comp = _competitor(env)
    video = _video(env, comp["id"])
    with pytest.raises(ValueError):
        store.set_analysis_status(video["id"], "doing_stuff")
    assert store.set_analysis_status("does-not-exist", "queued") is None
    with pytest.raises(StyleLabScopeError):
        store.set_analysis_status(
            video["id"], "queued", channel_id=env["channel_b"]
        )
    # The refused cross-channel write changed nothing.
    assert store.get_video(video["id"])["analysis_status"] == "not_queued"


# ---------------------------------------------------------------------------
# Style profiles
# ---------------------------------------------------------------------------


def test_create_and_list_profiles(env):
    store = env["store"]
    c1 = _competitor(env, raw="@AlphaOne")
    c2 = _competitor(env, raw="@BetaTwo")
    profile = store.create_profile(
        env["channel_a"], "Facts style", [c1["id"], c2["id"]]
    )
    assert profile["channel_id"] == env["channel_a"]
    assert profile["name"] == "Facts style"
    assert profile["status"] == "draft"
    assert profile["version"] == 1
    assert profile["competitor_ids"] == [c1["id"], c2["id"]]
    assert profile["profile"] is None

    assert [p["id"] for p in store.list_profiles(env["channel_a"])] == [
        profile["id"]
    ]
    assert store.list_profiles(env["channel_b"]) == []
    assert store.get_profile(profile["id"])["id"] == profile["id"]


def test_create_profile_rejects_foreign_and_unknown_competitors(env):
    store = env["store"]
    foreign = _competitor(env, channel=env["channel_b"], raw="@ForeignOne")
    with pytest.raises(StyleLabScopeError):
        store.create_profile(env["channel_a"], "bad", [foreign["id"]])
    with pytest.raises(StyleLabNotFoundError):
        store.create_profile(env["channel_a"], "bad", ["does-not-exist"])
    # Neither failed attempt created a profile.
    assert store.list_profiles(env["channel_a"]) == []


def test_activate_profile_supersedes_previous(env):
    store = env["store"]
    comp = _competitor(env)
    p1 = store.create_profile(env["channel_a"], "v1", [comp["id"]])
    p2 = store.create_profile(env["channel_a"], "v2", [comp["id"]])

    active1 = store.activate_profile(p1["id"])
    assert active1["status"] == "active"

    active2 = store.activate_profile(p2["id"])
    assert active2["status"] == "active"

    profiles = {p["id"]: p for p in store.list_profiles(env["channel_a"])}
    assert profiles[p1["id"]]["status"] == "superseded"
    assert profiles[p2["id"]]["status"] == "active"
    # Exactly one active profile per channel.
    assert [p for p in profiles.values() if p["status"] == "active"] == [
        profiles[p2["id"]]
    ]


def test_profile_cross_channel_isolation(env):
    store = env["store"]
    comp = _competitor(env)
    profile = store.create_profile(env["channel_a"], "mine", [comp["id"]])

    assert store.get_profile(profile["id"], channel_id=env["channel_b"]) is None
    with pytest.raises(StyleLabScopeError):
        store.activate_profile(profile["id"], channel_id=env["channel_b"])
    assert store.get_profile(profile["id"])["status"] == "draft"
    assert store.activate_profile("does-not-exist") is None


def test_store_accepts_bare_connection(env):
    # The store also works over a single shared connection.
    raw_conn = env["factory"]()
    try:
        shared = StyleLabStore(raw_conn)
        row = shared.create_competitor(
            env["channel_a"], parse_competitor_input("@SharedConn")
        )
        assert shared.get_competitor(row["id"]) is not None
        # And the factory-backed store sees the same row.
        assert env["store"].get_competitor(row["id"]) is not None
    finally:
        raw_conn.close()
