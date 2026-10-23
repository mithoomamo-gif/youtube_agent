"""Style Lab input parsing: the full accept / reject matrix.

Covers every form in the shared contract: channel URLs (www / m /
music subdomains), /@Handle URLs, bare @handles, bare UC channel ids,
legacy /c/ and /user/ URLs, and video URLs (watch / youtu.be / shorts /
live) — plus the rejection matrix (non-YouTube hosts, empty input,
garbage, malformed ids) with machine-readable error reasons.
"""

from __future__ import annotations

import pytest

from app.style_lab.parsing import (
    REASON_EMPTY_INPUT,
    REASON_INVALID_CHANNEL_ID,
    REASON_INVALID_HANDLE,
    REASON_INVALID_URL,
    REASON_INVALID_VIDEO_ID,
    REASON_NOT_YOUTUBE_HOST,
    REASON_UNRECOGNISED_INPUT,
    REASON_UNSUPPORTED_URL,
    StyleLabInputError,
    handle_key,
    normalise_handle,
    parse_competitor_input,
)

CHANNEL_ID = "UC" + "a" * 22  # UC + 22 chars
VIDEO_ID = "dQw4w9WgXcQ"  # 11 chars


# ---------------------------------------------------------------------------
# Channel URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/channel/{CHANNEL_ID}",
        f"http://www.youtube.com/channel/{CHANNEL_ID}",
        f"https://youtube.com/channel/{CHANNEL_ID}",
        f"https://m.youtube.com/channel/{CHANNEL_ID}",
        f"https://music.youtube.com/channel/{CHANNEL_ID}",
        f"https://www.youtube.com/channel/{CHANNEL_ID}/videos",
        f"https://www.youtube.com/channel/{CHANNEL_ID}?sub_confirmation=1",
    ],
)
def test_channel_url_accepted(url):
    parsed = parse_competitor_input(url)
    assert parsed.source_kind == "channel_url"
    assert parsed.external_channel_id == CHANNEL_ID
    assert parsed.handle is None
    assert parsed.external_video_id is None
    # Canonicalised to the www host regardless of the input subdomain.
    assert parsed.channel_url == f"https://www.youtube.com/channel/{CHANNEL_ID}"


@pytest.mark.parametrize(
    "url, handle",
    [
        ("https://www.youtube.com/@CoolChannel", "CoolChannel"),
        ("https://m.youtube.com/@CoolChannel", "CoolChannel"),
        ("https://www.youtube.com/@cool.channel_1/videos", "cool.channel_1"),
        ("https://www.youtube.com/@CoolChannel/", "CoolChannel"),
        ("www.youtube.com/@CoolChannel", "CoolChannel"),  # scheme-less
    ],
)
def test_handle_url_accepted(url, handle):
    parsed = parse_competitor_input(url)
    assert parsed.source_kind == "channel_url"
    assert parsed.handle == handle
    assert parsed.external_channel_id is None
    assert parsed.channel_url == f"https://www.youtube.com/@{handle}"


# ---------------------------------------------------------------------------
# Bare forms
# ---------------------------------------------------------------------------


def test_bare_handle():
    parsed = parse_competitor_input("@CoolChannel")
    assert parsed.source_kind == "handle"
    assert parsed.handle == "CoolChannel"  # '@' stripped, case preserved
    assert parsed.channel_url == "https://www.youtube.com/@CoolChannel"
    assert parsed.external_channel_id is None


def test_bare_handle_surrounding_whitespace():
    parsed = parse_competitor_input("   @CoolChannel  \n")
    assert parsed.handle == "CoolChannel"
    assert parsed.source_input == "@CoolChannel"


def test_bare_channel_id():
    parsed = parse_competitor_input(CHANNEL_ID)
    assert parsed.source_kind == "channel_id"
    assert parsed.external_channel_id == CHANNEL_ID
    assert parsed.channel_url == f"https://www.youtube.com/channel/{CHANNEL_ID}"
    assert parsed.handle is None


# ---------------------------------------------------------------------------
# Legacy /c/ and /user/ URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, kind_path, name",
    [
        ("https://www.youtube.com/c/OldStyleName", "c", "OldStyleName"),
        ("https://www.youtube.com/user/OldStyleName", "user", "OldStyleName"),
        ("https://m.youtube.com/c/OldStyleName/videos", "c", "OldStyleName"),
    ],
)
def test_legacy_custom_urls(url, kind_path, name):
    parsed = parse_competitor_input(url)
    assert parsed.source_kind == "custom_url"
    assert parsed.handle == name
    assert parsed.external_channel_id is None
    assert parsed.channel_url == f"https://www.youtube.com/{kind_path}/{name}"


# ---------------------------------------------------------------------------
# Video URLs — channel fields stay None (resolved later by metadata fetch)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/watch?t=42s&v={VIDEO_ID}&list=PL123",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?t=42",
        f"youtu.be/{VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}?feature=share",
    ],
)
def test_video_urls(url):
    parsed = parse_competitor_input(url)
    assert parsed.source_kind == "video_url"
    assert parsed.external_video_id == VIDEO_ID
    # A video URL alone cannot identify the channel.
    assert parsed.external_channel_id is None
    assert parsed.handle is None
    assert parsed.channel_url is None


# ---------------------------------------------------------------------------
# Handle helpers
# ---------------------------------------------------------------------------


def test_handle_helpers():
    assert normalise_handle("@CoolChannel") == "CoolChannel"
    assert normalise_handle("CoolChannel") == "CoolChannel"
    assert handle_key("@CoolChannel") == "coolchannel"
    assert handle_key(None) is None


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def _reason(raw):
    with pytest.raises(StyleLabInputError) as excinfo:
        parse_competitor_input(raw)
    return excinfo.value.reason


def test_error_is_a_value_error():
    with pytest.raises(ValueError):
        parse_competitor_input("")


@pytest.mark.parametrize("raw", ["", "   ", "\n\t ", None])
def test_empty_input_rejected(raw):
    assert _reason(raw) == REASON_EMPTY_INPUT


@pytest.mark.parametrize(
    "raw",
    [
        "https://vimeo.com/12345",
        "https://www.dailymotion.com/video/x123",
        f"https://youtube.com.evil.com/channel/{CHANNEL_ID}",
        "https://notyoutube.com/@CoolChannel",
        "https://www.google.com/search?q=youtube",
    ],
)
def test_non_youtube_hosts_rejected(raw):
    assert _reason(raw) == REASON_NOT_YOUTUBE_HOST


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.youtube.com/",
        "https://www.youtube.com/feed/subscriptions",
        "https://www.youtube.com/results?search_query=cool",
        "https://www.youtube.com/playlist?list=PL123",
    ],
)
def test_unsupported_youtube_urls_rejected(raw):
    assert _reason(raw) == REASON_UNSUPPORTED_URL


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.youtube.com/channel/UCtooshort",
        f"https://www.youtube.com/channel/{CHANNEL_ID}extra",
        "UCtooshort",
        "UC" + "a" * 21,
        "UC" + "a" * 23,
    ],
)
def test_invalid_channel_ids_rejected(raw):
    assert _reason(raw) == REASON_INVALID_CHANNEL_ID


@pytest.mark.parametrize(
    "raw",
    [
        "@ab",  # too short (< 3)
        "@" + "a" * 31,  # too long (> 30)
        "@bad handle!",  # spaces / punctuation outside the handle alphabet
        "https://www.youtube.com/@ab",
        "https://www.youtube.com/c/",
    ],
)
def test_invalid_handles_rejected(raw):
    # /c/ with no name is an unsupported URL rather than a bad handle.
    expected = (
        REASON_UNSUPPORTED_URL if raw.endswith("/c/") else REASON_INVALID_HANDLE
    )
    assert _reason(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?t=10",
        "https://www.youtube.com/watch?v=short",
        "https://youtu.be/",
        "https://youtu.be/waytoolongvideoid",
        "https://www.youtube.com/shorts/short",
        "https://www.youtube.com/live/",
    ],
)
def test_invalid_video_ids_rejected(raw):
    assert _reason(raw) == REASON_INVALID_VIDEO_ID


@pytest.mark.parametrize(
    "raw",
    [
        "just some garbage text",
        "CoolChannel",  # bare name without '@' is not a handle form
        "channel name here",
    ],
)
def test_garbage_rejected(raw):
    assert _reason(raw) == REASON_UNRECOGNISED_INPUT


def test_non_http_scheme_rejected():
    assert _reason("ftp://www.youtube.com/@CoolChannel") == REASON_INVALID_URL
