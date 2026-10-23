"""Report endpoints (Agent 31 — receipts + weekly digest, Round 5).

Routes::

    POST /api/v1/reports/generate                      -> 201 artefact (JSON)
    GET  /api/v1/reports?channel_id=&scope=&kind=      -> {items, next_cursor}
    GET  /api/v1/reports/{report_id}                   -> artefact (JSON: data + markdown)
    GET  /api/v1/reports/{report_id}/markdown          -> text/markdown
    GET  /api/v1/channels/{channel_id}/reports         -> {items, next_cursor}
    GET  /api/v1/channels/{channel_id}/reports/{report_id}          -> artefact (JSON)
    GET  /api/v1/channels/{channel_id}/reports/{report_id}/markdown -> text/markdown

All generation and reads go through the service contract in
:mod:`app.reports` (``generate_report`` / ``list_reports`` /
``get_report``).  Isolation follows the codebase convention (see
:mod:`app.routers.style_lab`): a report owned by channel A fetched
under channel B answers **404, never 403** — on the nested routes,
on the list (a foreign channel's reports simply never appear), and
on the top-level fetch when a ``channel_id`` query filter is given
that does not own the report.

Generation triggers: ``scope='video', kind='receipt'`` (per-video
receipt, ``video_id`` = project id or YouTube video id),
``scope='channel', kind='receipt'`` (per-channel receipt) and
``scope='channel', kind='weekly_digest'`` (weekly digest).  An
optional ``period`` of ``{start_ms, end_ms}`` pins the window;
defaults are the video's lifetime / trailing 30 days / trailing
7 days respectively.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.errors import ApiError
from app.reports import service

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])
channel_router = APIRouter(prefix="/api/v1/channels", tags=["reports"])


class GenerateReportRequest(BaseModel):
    scope: str
    kind: str = "receipt"
    channel_id: str
    video_id: Optional[str] = None
    project_id: Optional[str] = None  # alias for video_id
    period: Optional[dict[str, Any]] = None
    period_start_ms: Optional[int] = None
    period_end_ms: Optional[int] = None


def _generate(body: GenerateReportRequest) -> dict:
    period = body.period
    if period is None and (
        body.period_start_ms is not None or body.period_end_ms is not None
    ):
        period = {
            "start_ms": body.period_start_ms,
            "end_ms": body.period_end_ms,
        }
    return service.generate_report(
        body.scope,
        body.channel_id,
        video_id=body.video_id or body.project_id,
        period=period,
        kind=body.kind,
    )


def _fetch(report_id: str, channel_id: Optional[str]) -> dict:
    artefact = service.get_report(report_id, channel_id=channel_id)
    if artefact is None:
        raise ApiError(404, "report_not_found", f"report {report_id} was not found")
    return artefact


# ---------------------------------------------------------------------------
# Top-level routes
# ---------------------------------------------------------------------------


@router.post("/generate", status_code=201)
async def generate(body: GenerateReportRequest) -> dict:
    return _generate(body)


@router.get("")
async def list_reports(
    channel_id: Optional[str] = Query(default=None),
    scope: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
) -> dict:
    items = service.list_reports(channel_id, scope=scope, kind=kind)
    return {"items": items, "next_cursor": None}


@router.get("/{report_id}")
async def get_report(
    report_id: str, channel_id: Optional[str] = Query(default=None)
) -> dict:
    return _fetch(report_id, channel_id)


@router.get("/{report_id}/markdown")
async def get_report_markdown(
    report_id: str, channel_id: Optional[str] = Query(default=None)
) -> PlainTextResponse:
    return PlainTextResponse(
        _fetch(report_id, channel_id)["markdown"], media_type="text/markdown"
    )


# ---------------------------------------------------------------------------
# Channel-nested routes (strict owner scoping)
# ---------------------------------------------------------------------------


@channel_router.get("/{channel_id}/reports")
async def list_channel_reports(
    channel_id: str,
    scope: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
) -> dict:
    items = service.list_reports(channel_id, scope=scope, kind=kind)
    return {"items": items, "next_cursor": None}


@channel_router.get("/{channel_id}/reports/{report_id}")
async def get_channel_report(channel_id: str, report_id: str) -> dict:
    return _fetch(report_id, channel_id)


@channel_router.get("/{channel_id}/reports/{report_id}/markdown")
async def get_channel_report_markdown(
    channel_id: str, report_id: str
) -> PlainTextResponse:
    return PlainTextResponse(
        _fetch(report_id, channel_id)["markdown"], media_type="text/markdown"
    )
