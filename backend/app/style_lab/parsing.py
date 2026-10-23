"""Parsing of user-supplied competitor inputs (Style Lab intake).

Pure functions, no I/O.  :func:`parse_competitor_input` turns one raw
user string — a YouTube channel URL, a bare ``@Handle``, a bare channel
id, a legacy ``/c/`` or ``/user/`` URL, or a video URL — into a
:class:`ParsedCompetitor`.

Accepted forms
--------------
===============================  ====================  ==========================
input                            source_kind           identifiers set
===============================  ====================  ==========================
``youtube.com/channel/UC…``      ``channel_url``       external_channel_id
``youtube.com/@Handle``          ``channel_url``       handle
``@Handle`` (bare)               ``handle``            handle
``UC…`` (bare, UC + 22 chars)    ``channel_id``        external_channel_id
``youtube.com/c/<name>``         ``custom_url``        handle = <name>
``youtube.com/user/<name>``      ``custom_url``        handle = <name>
``youtube.com/watch?v=<id>``     ``video_url``         external_video_id
``youtu.be/<id>``                ``video_url``         external_video_id
``youtube.com/shorts/<id>``      ``video_url``         external_video_id
``youtube.com/live/<id>``        ``video_url``         external_video_id
===============================  ====================  ==========================

Hosts accepted: ``youtube.com``, ``www.youtube.com``,
``m.youtube.com``, ``music.youtube.com`` and ``youtu.be`` (http or
https; a missing scheme is tolerated for those hosts).  Channel URLs
are normalised to the canonical ``https://www.youtube.com/...`` form.

**A video URL alone cannot identify its channel.**  For
``source_kind == 'video_url'`` the channel fields
(``external_channel_id`` / ``handle`` / ``channel_url``) are all
``None``; which competitor channel the video belongs to is resolved
later, by the metadata-fetch step, from the video's own metadata.

Handles: the leading ``@`` is stripped and the handle is stored
as-given (case preserved).  This codebase has no lowercased-handle
column convention, so comparisons that must be case-insensitive (the
store's dedupe) lowercase at comparison time — see
:func:`handle_key`.

Rejections raise :class:`StyleLabInputError` (a ``ValueError``) with a
machine-readable ``reason`` attribute, one of the ``REASON_*``
constants below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

__all__ = [
    "ParsedCompetitor",
    "StyleLabInputError",
    "parse_competitor_input",
    "normalise_handle",
    "handle_key",
    "SOURCE_KIND_CHANNEL_URL",
    "SOURCE_KIND_HANDLE",
    "SOURCE_KIND_CHANNEL_ID",
    "SOURCE_KIND_CUSTOM_URL",
    "SOURCE_KIND_VIDEO_URL",
    "ALL_SOURCE_KINDS",
    "REASON_EMPTY_INPUT",
    "REASON_NOT_YOUTUBE_HOST",
    "REASON_INVALID_URL",
    "REASON_UNSUPPORTED_URL",
    "REASON_INVALID_CHANNEL_ID",
    "REASON_INVALID_HANDLE",
    "REASON_INVALID_VIDEO_ID",
    "REASON_UNRECOGNISED_INPUT",
]

# -- source kinds (mirror the competitor_channels.source_kind CHECK) ---------
SOURCE_KIND_CHANNEL_URL = "channel_url"
SOURCE_KIND_HANDLE = "handle"
SOURCE_KIND_CHANNEL_ID = "channel_id"
SOURCE_KIND_CUSTOM_URL = "custom_url"
SOURCE_KIND_VIDEO_URL = "video_url"

ALL_SOURCE_KINDS = (
    SOURCE_KIND_CHANNEL_URL,
    SOURCE_KIND_HANDLE,
    SOURCE_KIND_CHANNEL_ID,
    SOURCE_KIND_CUSTOM_URL,
    SOURCE_KIND_VIDEO_URL,
)

# -- machine-readable rejection reasons ---------------------------------------
REASON_EMPTY_INPUT = "empty_input"
REASON_NOT_YOUTUBE_HOST = "not_youtube_host"
REASON_INVALID_URL = "invalid_url"
REASON_UNSUPPORTED_URL = "unsupported_url"
REASON_INVALID_CHANNEL_ID = "invalid_channel_id"
REASON_INVALID_HANDLE = "invalid_handle"
REASON_INVALID_VIDEO_ID = "invalid_video_id"
REASON_UNRECOGNISED_INPUT = "unrecognised_input"

_CANONICAL_BASE = "https://www.youtube.com"

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }
)
_SHORT_HOST = "youtu.be"

# UC + exactly 22 chars from the URL-safe id alphabet.
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
# Video ids are 11 chars from the same alphabet.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# YouTube handles: 3-30 chars, letters/digits plus '.', '-' and '_'.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{3,30}$")
# Bare token that *looks* like an attempted channel id (UC-prefixed).
_CHANNEL_ID_ATTEMPT_RE = re.compile(r"^UC\S*$")


class StyleLabInputError(ValueError):
    """A competitor input string could not be parsed.

    ``reason`` is a machine-readable code (one of the ``REASON_*``
    constants); the exception message is the human-readable version.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ParsedCompetitor:
    """The parsed shape of one competitor input string.

    ``source_input`` is the user's raw string (stripped), carried so
    the store can record it verbatim on the ``competitor_channels``
    row; it is provenance, not an identifier.
    """

    source_kind: str
    external_channel_id: Optional[str] = None
    handle: Optional[str] = None
    channel_url: Optional[str] = None
    external_video_id: Optional[str] = None
    source_input: Optional[str] = None


def normalise_handle(handle: str) -> str:
    """Strip whitespace and one leading ``@``; case is preserved."""
    text = handle.strip()
    if text.startswith("@"):
        text = text[1:]
    return text


def handle_key(handle: Optional[str]) -> Optional[str]:
    """Case-insensitive comparison form of a handle (or ``None``)."""
    if handle is None:
        return None
    return normalise_handle(handle).lower()


def _validate_handle(candidate: str, raw: str) -> str:
    handle = normalise_handle(candidate)
    if not _HANDLE_RE.match(handle):
        raise StyleLabInputError(
            REASON_INVALID_HANDLE,
            f"invalid YouTube handle {candidate!r} in input {raw!r}",
        )
    return handle


def _validate_channel_id(candidate: str, raw: str) -> str:
    if not _CHANNEL_ID_RE.match(candidate):
        raise StyleLabInputError(
            REASON_INVALID_CHANNEL_ID,
            f"invalid YouTube channel id {candidate!r} in input {raw!r}",
        )
    return candidate


def _validate_video_id(candidate: str, raw: str) -> str:
    if not _VIDEO_ID_RE.match(candidate):
        raise StyleLabInputError(
            REASON_INVALID_VIDEO_ID,
            f"invalid YouTube video id {candidate!r} in input {raw!r}",
        )
    return candidate


def _video_result(video_id: str, raw: str) -> ParsedCompetitor:
    # NOTE: a video URL alone cannot identify the channel — the channel
    # fields stay None on purpose; the metadata fetch resolves them.
    return ParsedCompetitor(
        source_kind=SOURCE_KIND_VIDEO_URL,
        external_video_id=video_id,
        source_input=raw,
    )


def _parse_youtube_path(parsed, raw: str) -> ParsedCompetitor:
    """Dispatch on the path of a youtube.com-family URL."""
    segments = [seg for seg in parsed.path.split("/") if seg != ""]

    if not segments:
        raise StyleLabInputError(
            REASON_UNSUPPORTED_URL,
            f"YouTube URL has no channel or video path: {raw!r}",
        )

    head = segments[0]

    if head == "channel" and len(segments) >= 2:
        channel_id = _validate_channel_id(segments[1], raw)
        return ParsedCompetitor(
            source_kind=SOURCE_KIND_CHANNEL_URL,
            external_channel_id=channel_id,
            channel_url=f"{_CANONICAL_BASE}/channel/{channel_id}",
            source_input=raw,
        )

    if head.startswith("@"):
        handle = _validate_handle(head, raw)
        return ParsedCompetitor(
            source_kind=SOURCE_KIND_CHANNEL_URL,
            handle=handle,
            channel_url=f"{_CANONICAL_BASE}/@{handle}",
            source_input=raw,
        )

    if head in ("c", "user") and len(segments) >= 2:
        name = _validate_handle(segments[1], raw)
        return ParsedCompetitor(
            source_kind=SOURCE_KIND_CUSTOM_URL,
            handle=name,
            channel_url=f"{_CANONICAL_BASE}/{head}/{name}",
            source_input=raw,
        )

    if head == "watch":
        values = parse_qs(parsed.query).get("v")
        if not values or not values[0]:
            raise StyleLabInputError(
                REASON_INVALID_VIDEO_ID,
                f"watch URL is missing its 'v' video id: {raw!r}",
            )
        return _video_result(_validate_video_id(values[0], raw), raw)

    if head in ("shorts", "live", "embed", "v"):
        if len(segments) < 2:
            raise StyleLabInputError(
                REASON_INVALID_VIDEO_ID,
                f"{head} URL is missing its video id: {raw!r}",
            )
        return _video_result(_validate_video_id(segments[1], raw), raw)

    raise StyleLabInputError(
        REASON_UNSUPPORTED_URL,
        f"unsupported YouTube URL path {parsed.path!r} in input {raw!r}",
    )


def parse_competitor_input(raw: str) -> ParsedCompetitor:
    """Parse one user-supplied competitor string.

    See the module docstring for the accepted forms.  Raises
    :class:`StyleLabInputError` on empty input, non-YouTube hosts and
    anything unrecognisable.
    """
    if raw is None:
        raise StyleLabInputError(REASON_EMPTY_INPUT, "competitor input is empty")
    text = str(raw).strip()
    if not text:
        raise StyleLabInputError(REASON_EMPTY_INPUT, "competitor input is empty")

    # -- bare forms (no URL) --------------------------------------------------
    if "://" not in text and "/" not in text:
        if text.startswith("@"):
            handle = _validate_handle(text, text)
            return ParsedCompetitor(
                source_kind=SOURCE_KIND_HANDLE,
                handle=handle,
                channel_url=f"{_CANONICAL_BASE}/@{handle}",
                source_input=text,
            )
        if _CHANNEL_ID_RE.match(text):
            return ParsedCompetitor(
                source_kind=SOURCE_KIND_CHANNEL_ID,
                external_channel_id=text,
                channel_url=f"{_CANONICAL_BASE}/channel/{text}",
                source_input=text,
            )
        if _CHANNEL_ID_ATTEMPT_RE.match(text):
            raise StyleLabInputError(
                REASON_INVALID_CHANNEL_ID,
                f"invalid YouTube channel id {text!r} "
                "(expected 'UC' followed by 22 characters)",
            )
        raise StyleLabInputError(
            REASON_UNRECOGNISED_INPUT,
            f"unrecognised competitor input {text!r} "
            "(expected a YouTube URL, an @handle or a UC channel id)",
        )

    # -- URL forms -------------------------------------------------------------
    candidate = text
    if "://" not in candidate:
        # Tolerate scheme-less input ("www.youtube.com/@Foo",
        # "youtu.be/<id>") by assuming https.
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise StyleLabInputError(
            REASON_INVALID_URL, f"could not parse URL {text!r}: {exc}"
        ) from exc

    if parsed.scheme not in ("http", "https"):
        raise StyleLabInputError(
            REASON_INVALID_URL,
            f"unsupported URL scheme {parsed.scheme!r} in input {text!r}",
        )

    host = (parsed.hostname or "").lower()
    if host == _SHORT_HOST:
        segments = [seg for seg in parsed.path.split("/") if seg != ""]
        if len(segments) != 1:
            raise StyleLabInputError(
                REASON_INVALID_VIDEO_ID,
                f"youtu.be URL must contain exactly one video id: {text!r}",
            )
        return _video_result(_validate_video_id(segments[0], text), text)
    if host not in _YOUTUBE_HOSTS:
        raise StyleLabInputError(
            REASON_NOT_YOUTUBE_HOST,
            f"not a YouTube host: {host or text!r}",
        )
    return _parse_youtube_path(parsed, text)
