"""Aggregate API router — mounted by app.main at the app root.

Every sub-router carries its own ``/api/v1/...`` prefix.  NOT included
here, by design (section 31): the health router (``/health/*``, mounted
separately by main) and the WebSocket router (mounted separately by
main; the socket is a notification channel, not part of the REST
surface).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.routers import (
    agents,
    analytics,
    analytics_sync,
    approvals,
    backup,
    channels,
    comments,
    compliance,
    diagnostics,
    jobs,
    learning,
    logs,
    niches,
    onboarding,
    projects,
    providers,
    publications,
    reports,
    schedules,
    settings,
    style_lab,
    system,
    usage,
)

router = APIRouter()

router.include_router(system.router)
router.include_router(onboarding.router)
router.include_router(channels.router)
router.include_router(analytics_sync.router)
router.include_router(analytics.router)
router.include_router(style_lab.router)
router.include_router(comments.router)
router.include_router(niches.router)
router.include_router(projects.router)
router.include_router(approvals.router)
router.include_router(learning.router)
router.include_router(jobs.router)
router.include_router(agents.router)
router.include_router(schedules.router)
router.include_router(providers.router)
router.include_router(settings.router)
router.include_router(compliance.router)
router.include_router(backup.router)
router.include_router(usage.router)
router.include_router(publications.router)
router.include_router(logs.router)
router.include_router(diagnostics.router)
router.include_router(reports.router)
router.include_router(reports.channel_router)

__all__ = ["router"]
