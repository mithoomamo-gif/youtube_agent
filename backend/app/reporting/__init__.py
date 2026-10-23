"""Reporting engine (Agent 31) — per-video & per-channel receipts.

Round 5, Worker 1: the core engine.  Reports are generated from
LEDGERS ONLY, emitted as JSON (the source of truth) + Markdown
(rendered from the JSON), and persisted as artefacts in the
``generated_reports`` table for the UI to list.

The public seam is :mod:`app.reporting.service` — see its module
docstring for the full W1/W2 contract (generate / build / list /
fetch functions W2's HTTP endpoints and weekly digest build on).
This package deliberately contains NO HTTP routers and NO digest
variant (W2 owns those) and touches no frontend code.
"""

from __future__ import annotations

from app.reporting.engine import ReportNotFoundError
from app.reporting.models import SCHEMA_VERSION, ReportDocument
from app.reporting.render import render_markdown
from app.reporting.service import (
    build_channel_report,
    build_video_report,
    generate_channel_report,
    generate_video_report,
    get_report,
    get_report_markdown,
    list_reports,
)

__all__ = [
    "SCHEMA_VERSION",
    "ReportDocument",
    "ReportNotFoundError",
    "render_markdown",
    "generate_video_report",
    "generate_channel_report",
    "build_video_report",
    "build_channel_report",
    "list_reports",
    "get_report",
    "get_report_markdown",
]
