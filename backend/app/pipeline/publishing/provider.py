"""YouTubeProvider interface — the ONLY thing publishing code depends on.

No real YouTube calls live here: production wires a Data API v3 client
behind this Protocol, tests wire a scripted FakeProvider. Quota note
(Section 25): videos.insert bills the Uploads bucket (100/day),
everything else here bills the shared 10,000-unit pool; videos.list
polls cost 1 unit each.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import InvalidPublishRequestError

PRIVACY_PRIVATE = "private"
PRIVACY_UNLISTED = "unlisted"
PRIVACY_PUBLIC = "public"


def validate_publish_at(privacy_status: str, publish_at: datetime | None) -> None:
    """Hard rule (Section 34): publishAt is honoured ONLY with private.

    publishAt + public is silently ignored by YouTube and the video goes
    live immediately, so the client wrapper REFUSES to build such a
    request at all.
    """
    if publish_at is not None and privacy_status != PRIVACY_PRIVATE:
        raise InvalidPublishRequestError(
            "publishAt may only be sent with privacyStatus='private'; "
            f"got privacyStatus={privacy_status!r}. YouTube would ignore "
            "publishAt and publish immediately."
        )


class VideoMetadata(BaseModel):
    """Snippet + status payload carried by insert / update requests."""

    model_config = ConfigDict(frozen=True)

    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category_id: str = "22"
    default_language: str | None = None
    default_audio_language: str | None = None
    privacy_status: str = PRIVACY_PRIVATE
    publish_at: datetime | None = None
    made_for_kids: bool = False
    contains_synthetic_media: bool = False
    embeddable: bool = True
    licence: str = "youtube"

    @model_validator(mode="after")
    def _publish_at_invariant(self) -> "VideoMetadata":
        validate_publish_at(self.privacy_status, self.publish_at)
        return self


class VideoUpdate(BaseModel):
    """Fields for videos.update. Only the fields set are sent."""

    model_config = ConfigDict(frozen=True)

    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    category_id: str | None = None
    privacy_status: str | None = None
    publish_at: datetime | None = None
    # Explicit sentinel: clearing publishAt (watcher cancel) must be
    # distinguishable from "leave publishAt alone".
    clear_publish_at: bool = False
    made_for_kids: bool | None = None
    contains_synthetic_media: bool | None = None

    @model_validator(mode="after")
    def _invariants(self) -> "VideoUpdate":
        if self.clear_publish_at and self.publish_at is not None:
            raise InvalidPublishRequestError(
                "cannot set and clear publishAt in the same update"
            )
        if self.publish_at is not None:
            # Scheduling via update MUST re-send privacyStatus=private
            # explicitly (Section 34, two-step scheduling step 3).
            if self.privacy_status != PRIVACY_PRIVATE:
                raise InvalidPublishRequestError(
                    "an update carrying publishAt must explicitly re-send "
                    "privacyStatus='private'"
                )
        return self


@dataclass(frozen=True)
class ChunkResult:
    """Result of one chunk PUT or of a session status query.

    status_code 308 = resume incomplete (confirmed_bytes is what the
    SERVER reports via its Range header — never a client assumption);
    200/201 = upload complete and video_id is present.
    """

    status_code: int
    confirmed_bytes: int = 0
    video_id: str | None = None
    retry_after_seconds: float | None = None

    @property
    def complete(self) -> bool:
        return self.status_code in (200, 201) and self.video_id is not None


_RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)")


def parse_range_header(range_header: str | None) -> int:
    """'bytes=0-1048575' -> 1048576 confirmed bytes. Absent header -> 0."""
    if not range_header:
        return 0
    m = _RANGE_RE.search(range_header)
    if not m:
        return 0
    return int(m.group(2)) + 1


class VideoStatus(BaseModel):
    """Owned-channel view of a video (status/processing/contentDetails)."""

    model_config = ConfigDict(frozen=True)

    video_id: str
    privacy_status: str = PRIVACY_PRIVATE
    publish_at: datetime | None = None
    published_at: datetime | None = None
    upload_status: str = "uploaded"  # uploaded | processed | failed | rejected
    processing_status: str = "processing"  # processing | succeeded | failed
    processing_failure_reason: str | None = None
    rejection_reason: str | None = None  # claim|copyright|duplicate|termsOfUse|...
    # Seconds; None = unknown, 0.0 = P0D (nothing really arrived).
    # NEVER use a file size for completeness: fileDetails.fileSize merely
    # echoes the size declared when the session was opened (Section 34).
    duration_seconds: float | None = None
    definition: str = "sd"  # sd | hd
    title: str = ""
    description: str = ""
    thumbnail_present: bool = False
    caption_languages: list[str] = Field(default_factory=list)
    made_for_kids: bool = False


@runtime_checkable
class YouTubeProvider(Protocol):
    """Every YouTube operation the publishing workflow needs."""

    def create_resumable_session(
        self, metadata: VideoMetadata, file_size: int
    ) -> str:
        """videos.insert initiation; returns the session URI (Location)."""
        ...

    def upload_chunk(
        self, session_uri: str, data: bytes, offset: int
    ) -> ChunkResult:
        """PUT one chunk; 308+Range while incomplete, 200/201 at the end."""
        ...

    def query_upload_status(self, session_uri: str) -> ChunkResult:
        """Empty PUT (Content-Range: bytes */*): what does the server hold?"""
        ...

    def set_thumbnail(self, video_id: str, thumbnail_path: str) -> None: ...

    def insert_captions(
        self, video_id: str, srt_path: str, language: str
    ) -> str:
        """captions.insert; returns the caption track id."""
        ...

    def add_to_playlist(self, playlist_id: str, video_id: str) -> str:
        """playlistItems.insert; returns the playlist item id."""
        ...

    def update_video(self, video_id: str, update: VideoUpdate) -> None: ...

    def get_video_status(self, video_id: str) -> VideoStatus: ...


def metadata_for_insert(
    *,
    title: str,
    description: str,
    tags: list[str],
    category_id: str,
    default_language: str | None,
    default_audio_language: str | None,
    made_for_kids: bool,
    contains_synthetic_media: bool,
    embeddable: bool = True,
    licence: str = "youtube",
) -> VideoMetadata:
    """Build the insert payload. Privacy is ALWAYS private, never a
    publishAt at insert: scheduling happens two-step, after checks pass.
    """
    return VideoMetadata(
        title=title,
        description=description,
        tags=list(tags),
        category_id=category_id,
        default_language=default_language,
        default_audio_language=default_audio_language,
        privacy_status=PRIVACY_PRIVATE,
        publish_at=None,
        made_for_kids=made_for_kids,
        contains_synthetic_media=contains_synthetic_media,
        embeddable=embeddable,
        licence=licence,
    )


def build_schedule_update(publish_at: datetime) -> VideoUpdate:
    """The only sanctioned way to arm a slot: private re-sent + publishAt."""
    return VideoUpdate(privacy_status=PRIVACY_PRIVATE, publish_at=publish_at)


def build_release_update() -> VideoUpdate:
    """Immediate release: public, with no publishAt anywhere near it."""
    return VideoUpdate(privacy_status=PRIVACY_PUBLIC)


def build_cancel_schedule_update() -> VideoUpdate:
    """Watcher cancel: back to plain private, publishAt cleared."""
    return VideoUpdate(privacy_status=PRIVACY_PRIVATE, clear_publish_at=True)


__all__ = [
    "ChunkResult",
    "VideoMetadata",
    "VideoStatus",
    "VideoUpdate",
    "YouTubeProvider",
    "build_cancel_schedule_update",
    "build_release_update",
    "build_schedule_update",
    "metadata_for_insert",
    "parse_range_header",
    "validate_publish_at",
    "PRIVACY_PRIVATE",
    "PRIVACY_PUBLIC",
    "PRIVACY_UNLISTED",
    "Any",
]
