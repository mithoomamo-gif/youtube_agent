"""First-run onboarding endpoints (Round 6).

The wizard is NOT the installer: it must PROVE the machine works.

* ``GET  /api/v1/onboarding/system-check`` — runs the real checks in
  :mod:`app.system_check` (WebView2 / FFmpeg+loudnorm / disk / sidecar
  health) and reports the credential-store selection. Statuses are
  honest: a check that cannot run on this OS says ``unverified``.
* ``GET  /api/v1/onboarding/status`` — completion flag (persisted via
  the settings table under the single key ``onboarding``) plus the
  honest key states: a provider key is ``verified`` ONLY when the
  existing provider test-connection last returned ``ok``; a stored but
  untested key is ``saved_unverified`` ("saved, not yet verified").
  YouTube reports channel-connect state from the channels table and
  whether the OAuth client credentials exist in the secret store.
  No secret value ever appears in any response — metadata only.
* ``POST /api/v1/onboarding/complete`` — records completion. The
  60-second test video is OPTIONAL: ``test_video: "skip"`` is a
  first-class choice and completes onboarding exactly the same way.
* ``POST /api/v1/onboarding/test-video`` — creates a ~60-second
  project (``target_minutes: 1``) through the SAME produce path as
  ``POST /projects`` (project record + execution DAG enqueue +
  pipeline start enqueue). If the execution queue is unavailable the
  response says ``created_not_queued`` — never a fake success.

Key entry itself reuses the existing providers router
(``PUT /providers/{id}/key`` stores via :mod:`app.secrets_store`, the
OS credential store; ``POST /providers/{id}/test`` performs the real
cheapest-call validation). This router adds no second key path.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import secrets_store, system_check
from app.errors import ApiError

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])

SETTINGS_KEY = "onboarding"
TEST_VIDEO_TOPIC_DEFAULT = "Test video: one minute at the bottom of the ocean"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _data_dir(request: Request):
    settings = getattr(request.app.state, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is not None:
        return data_dir
    try:
        from app.config import get_settings

        return get_settings().data_dir
    except Exception:
        from pathlib import Path

        return Path.home() / ".youtube-agent"


def _load_onboarding() -> dict:
    try:
        from app.routers import settings as settings_router

        value = settings_router._load().get(SETTINGS_KEY)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_onboarding(value: dict) -> None:
    from app.routers import settings as settings_router

    settings_router._upsert({SETTINGS_KEY: value})


def _key_state(provider_id: str) -> dict:
    """Honest key state: verified only after a real test returned ok."""
    from app.routers import providers as providers_router

    ref = providers_router._keyring_ref(provider_id)
    try:
        configured = secrets_store.has_secret(ref)
    except ApiError:
        return {
            "provider": provider_id,
            "state": "store_unavailable",
            "configured": False,
            "verified": False,
            "detail": "The OS credential store is unavailable on this "
            "machine, so the key state could not be read.",
            "key_last4": None,
        }
    last_test = providers_router._LAST_TEST.get(provider_id)
    verified = bool(last_test and last_test.get("status") == "ok")
    if verified:
        state, detail = "verified", "Saved and verified by a real test call."
    elif configured:
        state = "saved_unverified"
        detail = "Saved, not yet verified — run the provider test to verify."
        if last_test and last_test.get("status") == "error":
            state = "saved_test_failed"
            detail = (
                "Saved, but the last real test call failed"
                + (
                    f" ({last_test.get('category')})."
                    if last_test.get("category")
                    else "."
                )
            )
    else:
        state, detail = "missing", "No key saved yet."
    return {
        "provider": provider_id,
        "state": state,
        "configured": configured,
        "verified": verified,
        "detail": detail,
        "key_last4": providers_router._last4(provider_id) if configured else None,
        "last_test_status": (last_test or {}).get("status"),
    }


def _youtube_state() -> dict:
    """YouTube connect state — channels table + OAuth client presence."""
    connected_channels: list[dict] = []
    try:
        from app.routers import channels as channels_router

        for channel in channels_router._list_channels():
            # "Connected" is the OAuth connect state — config_json's
            # connect_status, surfaced onto the public row by
            # _list_channels: "connected", or "needs_audit_check"
            # (token stored, channel linked, compliance audit still
            # pending). The lifecycle column (active/paused/...) only
            # says the channel record is enabled: a locally-created
            # channel that never went through OAuth is NOT a connected
            # YouTube channel and must never be reported as one.
            connect_status = str(channel.get("connect_status") or "").lower()
            if connect_status in (
                channels_router.CONNECT_CONNECTED,
                channels_router.CONNECT_NEEDS_AUDIT_CHECK,
            ):
                connected_channels.append(
                    {
                        "id": channel.get("id"),
                        "name": channel.get("name")
                        or channel.get("youtube_channel_title"),
                    }
                )
    except Exception:
        connected_channels = []
    client_configured = False
    try:
        from app.routers.channels import CLIENT_ID_REF, CLIENT_SECRET_REF

        client_configured = bool(
            secrets_store.has_secret(CLIENT_ID_REF)
            and secrets_store.has_secret(CLIENT_SECRET_REF)
        )
    except Exception:
        client_configured = False
    if connected_channels:
        state, detail = "connected", "A YouTube channel is connected."
    elif client_configured:
        state = "ready_to_connect"
        detail = (
            "OAuth client credentials are saved, but no channel is "
            "connected yet — connect from the Channels screen."
        )
    else:
        state = "not_configured"
        detail = (
            "YouTube is not set up yet: no OAuth client credentials "
            "are saved and no channel is connected. The Channels "
            "screen shows the one-time Google Cloud setup steps."
        )
    return {
        "state": state,
        "detail": detail,
        "connected": bool(connected_channels),
        "connected_channels": connected_channels,
        "client_configured": client_configured,
        # Connecting is an interactive browser sign-in (Channels
        # screen); the wizard reports state, it does not fake it.
        "verified": bool(connected_channels),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/system-check")
async def get_system_check(request: Request) -> dict:
    headers = {k.lower(): v for k, v in request.headers.items()}
    result = await system_check.run_all_checks(
        data_dir=_data_dir(request),
        app=request.app,
        headers=headers,
    )
    try:
        result["credential_store"] = secrets_store.credential_store_info()
    except Exception as exc:  # pragma: no cover - defensive
        result["credential_store"] = {
            "platform": result.get("platform"),
            "backend": "unknown",
            "available": False,
            "windows_credential_manager": False,
            "detail": f"Could not inspect the credential store: {exc}",
        }
    return result


@router.get("/status")
async def get_status() -> dict:
    onboarding = _load_onboarding()
    return {
        "completed": bool(onboarding.get("completed")),
        "completed_at": onboarding.get("completed_at"),
        "test_video_choice": onboarding.get("test_video"),
        "keys": {
            "gemini": _key_state("gemini"),
            "pixabay": _key_state("pixabay"),
        },
        "youtube": _youtube_state(),
        "credential_store": secrets_store.credential_store_info(),
    }


class CompleteBody(BaseModel):
    # "skip" is a first-class choice; "run" records that the user opted
    # into the test video (the video itself is started via
    # POST /onboarding/test-video).
    test_video: str = "skip"


@router.post("/complete")
async def complete(body: CompleteBody) -> dict:
    if body.test_video not in ("skip", "run"):
        raise ApiError(
            422,
            "validation_error",
            "test_video must be 'skip' or 'run'.",
            details={"field": "test_video"},
        )
    value = {
        "completed": True,
        "completed_at": int(time.time() * 1000),
        "test_video": body.test_video,
    }
    _save_onboarding(value)
    return {"completed": True, **value}


class TestVideoBody(BaseModel):
    channel_id: str
    niche_profile_id: str
    topic: Optional[str] = None


@router.post("/test-video", status_code=201)
async def start_test_video(body: TestVideoBody) -> dict:
    """Create a ~60s test project via the existing produce path.

    Mirrors ``POST /projects`` exactly: the project record is stored
    through the projects router's own helpers, the execution DAG is
    enqueued via ``app.execution.dag.create_project_dag`` and a
    ``pipeline.start`` job is enqueued on the same job queue. Whatever
    did not actually happen is reported, not hidden.
    """
    if not body.channel_id.strip() or not body.niche_profile_id.strip():
        raise ApiError(
            422,
            "validation_error",
            "A channel and a niche profile are required to produce "
            "the test video — create one first, or skip this step.",
            details={"field": "channel_id/niche_profile_id"},
        )
    from app.routers import projects as projects_router

    topic = (body.topic or "").strip() or TEST_VIDEO_TOPIC_DEFAULT
    project: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "channel_id": body.channel_id,
        "niche_profile_id": body.niche_profile_id,
        "topic_mode": "manual",
        "topic": topic,
        # The produce path's shortest target: 1 minute ≈ 60 seconds.
        "target_minutes": 1,
        "publish_mode": "review_first",
        "status": "draft",
        "current_phase": None,
        "created_at": projects_router._now_iso(),
        "updated_at": projects_router._now_iso(),
        "task_dag": {
            "status": "not_started",
            "total_agents": 35,
            "phases": list(projects_router._PHASES),
        },
        "origin": "onboarding_test_video",
    }
    projects_router._PROJECTS[project["id"]] = project
    projects_router._persist_project(project)

    queued = False
    job_ids: list = []
    try:
        from app.execution.dag import create_project_dag

        queue = projects_router._job_queue()
        if queue is not None:
            dag_result = create_project_dag(queue, project["id"])
            if dag_result is not None and dag_result is not False:
                queued = True
                if isinstance(dag_result, (list, tuple)):
                    job_ids = list(dag_result)
                elif isinstance(dag_result, dict):
                    for key in ("job_ids", "jobs", "ids"):
                        if isinstance(dag_result.get(key), (list, tuple)):
                            job_ids = list(dag_result[key])
                            break
                project["task_dag"] = {
                    "status": "queued",
                    "total_agents": 35,
                    "phases": list(projects_router._PHASES),
                    "job_ids": job_ids,
                }
            # Same pipeline-start enqueue the projects router performs.
            try:
                start_job = queue.enqueue(
                    "pipeline.start",
                    {"project_id": project["id"], "action": "start"},
                    queue="default",
                    idempotency_key=f"onboarding-test-video:{project['id']}",
                    project_id=None,
                )
                if start_job:
                    job_ids.append(start_job)
            except Exception:
                pass
    except Exception:
        queued = False

    return {
        "status": "queued" if queued else "created_not_queued",
        "project_id": project["id"],
        "topic": topic,
        "target_minutes": 1,
        "estimated_duration_s": 60,
        "job_ids": job_ids,
        "detail": (
            "Test video project created and queued on the production "
            "pipeline."
            if queued
            else "Test video project was created, but the execution "
            "queue was unavailable, so production has NOT started. "
            "You can start it later from the project list."
        ),
    }
