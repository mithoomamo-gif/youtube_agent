"""Style Lab endpoints (competitor intake + style profiles).

All routes are nested under the OWNER channel — the user's own
channel the competitor / profile belongs to::

    POST /api/v1/channels/{channel_id}/competitors
    GET  /api/v1/channels/{channel_id}/competitors
    GET  /api/v1/channels/{channel_id}/competitors/{competitor_id}
    POST /api/v1/channels/{channel_id}/competitors/{competitor_id}/archive
    POST /api/v1/channels/{channel_id}/competitors/{competitor_id}/metadata/fetch
    GET  /api/v1/channels/{channel_id}/competitors/{competitor_id}/videos
    POST /api/v1/channels/{channel_id}/competitors/{competitor_id}/videos
    POST /api/v1/channels/{channel_id}/competitors/{competitor_id}/videos/attach
    POST /api/v1/channels/{channel_id}/competitor-videos/{video_id}/analysis
    POST /api/v1/channels/{channel_id}/style-profiles
    GET  /api/v1/channels/{channel_id}/style-profiles
    POST /api/v1/channels/{channel_id}/style-profiles/{profile_id}/activate

Conventions follow the sibling routers: the ``ApiError`` envelope
from :mod:`app.errors` for every failure, list responses as
``{"items": [...], "next_cursor": None}``, creates as 201, and the
launch-token middleware (mounted by ``app.main``) guards everything.

Isolation posture: every endpoint is owner-scoped through
:mod:`app.style_lab.service` — a competitor / video / profile under
another channel answers 404 (never 403), exactly like the rest of
the codebase, so existence never leaks across channels.

Video intake has two first-class paths: a FILE attach (multipart,
streamed to disk by the service — never held whole in memory) and a
METADATA attach (JSON: a video known by URL or pasted
title/description/tags), which is never blocked on a metadata fetch.
The metadata/fetch endpoint itself is honest: with no YouTube
credential configured it answers 200 with
``metadata_status='metadata_unavailable'``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from app.style_lab import service

router = APIRouter(prefix="/api/v1/channels", tags=["style_lab"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CompetitorCreate(BaseModel):
    """One competitor input.  ``input`` is the canonical field; the
    aliases cover the shapes a UI is likely to send (a bare URL, a
    handle field, ...).  ``display_name`` is an optional user label."""

    input: Optional[str] = None
    raw_input: Optional[str] = None
    url: Optional[str] = None
    channel_url: Optional[str] = None
    handle: Optional[str] = None
    display_name: Optional[str] = None

    def raw(self) -> str:
        for value in (self.input, self.raw_input, self.url,
                      self.channel_url, self.handle):
            if value and str(value).strip():
                return str(value)
        return ""


class VideoMetadataAttach(BaseModel):
    """A competitor video described by metadata only (no file)."""

    source: Optional[str] = None  # 'url_metadata' | 'pasted_metadata'
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    video_url: Optional[str] = None
    url: Optional[str] = None
    external_video_id: Optional[str] = None
    video_id: Optional[str] = None


class StyleProfileCreate(BaseModel):
    name: str
    competitor_ids: list[str] = []
    profile: Optional[dict[str, Any]] = None
    profile_json: Optional[Any] = None
    version: Optional[int] = None
    status: Optional[str] = None  # 'draft' (default) | 'active'


# ---------------------------------------------------------------------------
# Competitors
# ---------------------------------------------------------------------------


@router.post("/{channel_id}/competitors", status_code=201)
async def create_competitor(channel_id: str, body: CompetitorCreate) -> dict:
    return service.add_competitor(
        channel_id, body.raw(), display_name=body.display_name
    )


@router.get("/{channel_id}/competitors")
async def list_competitors(
    channel_id: str, include_archived: bool = False
) -> dict:
    items = service.list_competitors(
        channel_id, include_archived=include_archived
    )
    return {"items": items, "next_cursor": None}


@router.get("/{channel_id}/competitors/{competitor_id}")
async def get_competitor(channel_id: str, competitor_id: str) -> dict:
    return service.get_competitor(channel_id, competitor_id)


@router.post("/{channel_id}/competitors/{competitor_id}/archive")
async def archive_competitor(channel_id: str, competitor_id: str) -> dict:
    return service.archive_competitor(channel_id, competitor_id)


@router.post("/{channel_id}/competitors/{competitor_id}/metadata/fetch")
async def fetch_competitor_metadata(
    channel_id: str, competitor_id: str
) -> dict:
    return await service.fetch_metadata(channel_id, competitor_id)


# ---------------------------------------------------------------------------
# Competitor videos
# ---------------------------------------------------------------------------


@router.get("/{channel_id}/competitors/{competitor_id}/videos")
async def list_videos(channel_id: str, competitor_id: str) -> dict:
    items = service.list_videos(channel_id, competitor_id)
    return {"items": items, "next_cursor": None}


@router.post(
    "/{channel_id}/competitors/{competitor_id}/videos", status_code=201
)
async def attach_video_metadata(
    channel_id: str, competitor_id: str, body: VideoMetadataAttach
) -> dict:
    return service.attach_video_metadata(
        channel_id, competitor_id, body.model_dump()
    )


@router.post(
    "/{channel_id}/competitors/{competitor_id}/videos/attach",
    status_code=201,
)
async def attach_video_file(
    channel_id: str,
    competitor_id: str,
    video: UploadFile = File(...),
    thumbnail: Optional[UploadFile] = File(None),
    title: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
) -> dict:
    """Multipart file attach: ``video`` file + optional ``thumbnail``
    file (+ optional ``title`` / ``description`` form fields).  The
    service streams both files to disk — no whole-file memory load."""
    return await service.attach_video_file(
        channel_id,
        competitor_id,
        video,
        thumbnail_upload=thumbnail,
        title=title,
        description=description,
    )


@router.post("/{channel_id}/competitor-videos/{video_id}/analysis")
async def request_analysis(channel_id: str, video_id: str) -> dict:
    """Queue a video for analysis (the intake -> engine seam: this
    flips ``analysis_status`` to ``'queued'``; the analysis engine
    claims queued rows)."""
    return service.request_analysis(channel_id, video_id)


# ---------------------------------------------------------------------------
# Style profiles
# ---------------------------------------------------------------------------


@router.post("/{channel_id}/style-profiles", status_code=201)
async def create_style_profile(
    channel_id: str, body: StyleProfileCreate
) -> dict:
    # SL-001: the service gates a supplied profile payload BEFORE
    # persisting anything — strict style_profile.v1 schema validation
    # + assert_no_copy against the listed competitors' stored titles /
    # descriptions (422 invalid_style_profile / no_copy_violation,
    # nothing persisted on rejection). See app.style_lab.service.
    profile_payload = body.profile if body.profile is not None else body.profile_json
    return service.create_profile(
        channel_id,
        name=body.name,
        competitor_ids=body.competitor_ids,
        profile=profile_payload,
        version=body.version,
        activate=(body.status == "active"),
    )


@router.get("/{channel_id}/style-profiles")
async def list_style_profiles(channel_id: str) -> dict:
    items = service.list_profiles(channel_id)
    return {"items": items, "next_cursor": None}


@router.post("/{channel_id}/style-profiles/{profile_id}/activate")
async def activate_style_profile(channel_id: str, profile_id: str) -> dict:
    return service.activate_profile(channel_id, profile_id)
