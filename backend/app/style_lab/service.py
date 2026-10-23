"""Style Lab intake service — orchestration between the REST router,
the sibling Style Lab modules and the analysis engine.

This module is the ONLY place the router talks to; it composes:

* :mod:`app.style_lab.parsing` — ``parse_competitor_input`` turns a raw
  user string (channel URL / ``@handle`` / UC id / video URL) into a
  :class:`~app.style_lab.parsing.ParsedCompetitor`;
  :class:`~app.style_lab.parsing.StyleLabInputError` becomes a 422
  ``invalid_competitor_input`` ApiError carrying the machine-readable
  ``reason``.
* :mod:`app.style_lab.store` — :class:`StyleLabStore` owns ALL
  competitor / video / profile persistence, dedupe and owner scoping.
  This service adds the two things the store deliberately does not do:
  resolving the owner channel row (404 when it does not exist) and
  persisting metadata-fetch results (the store has no update API, so
  those two writes — ``competitor_channels`` metadata columns and an
  optional ``style_profiles.profile_json`` / ``version`` supplied at
  create time — are done here, scoped by ``channel_id`` in the WHERE
  clause so they can never touch another channel's rows).
* :mod:`app.style_analysis.media` — media validation / storage for attached
  files (``probe_media`` / ``store_video_file`` / ``store_thumbnail``,
  typed :class:`MediaValidationError`).  Uploads are STREAMED to a
  spool file in 1 MiB chunks by this service (never held whole in
  memory); the media module then validates by probed content and
  stores content-addressed under
  ``<data_dir>/style_lab/<owner>/<competitor>/<sha256><ext>``.  If the
  media module is unavailable the service falls back to a local
  ffprobe probe with the same contract, so intake never accepts an
  unprobed file.
* :mod:`app.style_lab.metadata` — ``fetch_channel_metadata`` (async).
  With no usable YouTube credential it returns
  ``metadata_unavailable`` WITHOUT any HTTP call; this service
  persists whatever honest state comes back (``fetched`` /
  ``metadata_unavailable`` / ``fetch_failed``) and returns it.

Owner scoping / isolation posture
---------------------------------
Every operation first resolves the owner channel (``channels.id``; a
YouTube channel id is also accepted and resolved to the internal id,
mirroring ``app.routers.channels``).  Unknown owner -> 404
``channel_not_found``.  A competitor / video / profile that exists
under a DIFFERENT channel is reported as 404, never 403 — existence
must not leak across channels (the store enforces the same rule: its
scoped reads return ``None`` and its scoped writes raise
``StyleLabScopeError``, which this service maps to 404).

THE NO-COPY GATE (SL-001)
-------------------------
A style profile supplied at create time is CONTENT, and content is
gated BEFORE anything is persisted: it must validate against the
strict ``style_profile.v1`` schema
(:class:`app.integration.style_profile.StyleProfile`, the exact model
the synthesis layer uses) and it must pass
:func:`app.integration.style_profile.assert_no_copy` against the
competitor source material (video titles / descriptions, plus any
fetched video metadata) stored for the competitors the profile claims
to derive from.  A schema-invalid or copy-violating payload is a 422
and leaves NO row behind.  A create with NO profile payload persists
no profile content at all (``profile_json`` stays NULL), so there is
nothing to gate.  Activation re-checks a stored content-bearing
profile the same way, so a legacy row written before this gate
existed cannot be activated while it violates it.

THE ANALYSIS SEAM
-----------------
:func:`request_analysis` is the hand-off point between intake and the
analysis engine (``app.style_analysis``).  It performs exactly one write:
``competitor_videos.analysis_status`` -> ``'queued'`` (which also
stamps ``analysis_requested_at``).  The analysis engine claims rows
in ``'queued'`` state, transitions them through ``'analysing'`` to
``'analysed'`` / ``'analysis_failed'`` and writes its results
elsewhere.  This service NEVER runs analysis itself, and no other
function here mutates ``analysis_status``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from app.errors import ApiError
from app.ffmpeg_resolution import ffprobe_executable
from app.integration.style_profile import (
    CompetitorVideoAnalysis,
    NoCopyViolationError,
    StyleProfile,
    assert_no_copy,
)
from app.style_lab.parsing import StyleLabInputError, parse_competitor_input
from app.style_lab.schema import ensure_style_lab_schema
from app.style_lab.store import (
    StyleLabNotFoundError,
    StyleLabScopeError,
    StyleLabStore,
)

logger = logging.getLogger(__name__)

__all__ = [
    "add_competitor",
    "list_competitors",
    "get_competitor",
    "archive_competitor",
    "list_videos",
    "attach_video_metadata",
    "attach_video_file",
    "fetch_metadata",
    "request_analysis",
    "create_profile",
    "list_profiles",
    "activate_profile",
    "MediaValidationError",
]

# ---------------------------------------------------------------------------
# Sibling media module (app.style_analysis.media; imported defensively)
# ---------------------------------------------------------------------------

_media_module = None
for _candidate in ("app.style_analysis.media", "app.style_lab.media"):
    try:
        import importlib

        _media_module = importlib.import_module(_candidate)
        break
    except Exception:  # noqa: BLE001
        continue

_SiblingMediaError = getattr(_media_module, "MediaValidationError", None)


class _LocalMediaValidationError(ValueError):
    """Fallback media error mirroring the media module's typed error."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.code = reason


#: The media error type this service translates.  When the sibling
#: media module is present this IS its MediaValidationError.
MediaValidationError = _SiblingMediaError or _LocalMediaValidationError


def _media():
    """The sibling media module, re-imported lazily if it landed late."""
    global _media_module
    if _media_module is None:
        import importlib

        for candidate in ("app.style_analysis.media", "app.style_lab.media"):
            try:
                _media_module = importlib.import_module(candidate)
                break
            except Exception:  # noqa: BLE001
                continue
    return _media_module


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHUNK = 1024 * 1024  # 1 MiB streaming chunks for uploads
#: Size caps mirror app.style_analysis.media when it is present (2 GiB video,
#: 16 MiB thumbnail); the literals are the same values as a fallback.
MAX_VIDEO_BYTES = int(
    getattr(_media_module, "MAX_VIDEO_BYTES", 2 * 1024 * 1024 * 1024)
)
MAX_THUMBNAIL_BYTES = int(
    getattr(_media_module, "MAX_THUMBNAIL_BYTES", 16 * 1024 * 1024)
)

_VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_EXT_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Connection / store plumbing
# ---------------------------------------------------------------------------


def _conn_factory():
    from app import db as db_module

    return db_module.get_connection


def _store() -> StyleLabStore:
    return StyleLabStore(_conn_factory())


def _new_conn():
    from app import db as db_module

    conn = db_module.get_connection()
    ensure_style_lab_schema(conn)
    return conn


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _data_dir() -> Path:
    try:
        from app.config import get_settings

        return Path(get_settings().data_dir)
    except Exception:  # noqa: BLE001
        from app.data_dir import resolve_data_dir

        return resolve_data_dir()


# ---------------------------------------------------------------------------
# Owner-channel resolution + scope helpers
# ---------------------------------------------------------------------------


def resolve_channel_id(channel_id: str) -> str:
    """Resolve an owner channel reference to its internal ``channels.id``.

    Accepts the internal id or the YouTube channel id (the channels
    router's lookup convention).  Raises 404 ``channel_not_found`` when
    no live channel matches — every Style Lab operation starts here,
    so nothing can be created under a non-existent owner.
    """
    conn = _new_conn()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
        where_deleted = "AND deleted_at IS NULL" if "deleted_at" in cols else ""
        row = conn.execute(
            "SELECT id FROM channels WHERE (id = ? OR youtube_channel_id = ?)"
            f" {where_deleted} LIMIT 1",
            (str(channel_id), str(channel_id)),
        ).fetchone()
        if row is None:
            raise ApiError(
                404, "channel_not_found", f"No channel with id '{channel_id}'."
            )
        return str(row[0])
    finally:
        _close(conn)


def _not_found(what: str, ident: Any) -> ApiError:
    codes = {
        "competitor": "competitor_not_found",
        "video": "competitor_video_not_found",
        "profile": "style_profile_not_found",
    }
    return ApiError(404, codes[what], f"No {what} with id '{ident}'.")


def _require_competitor(store: StyleLabStore, channel_pk: str, competitor_id: str) -> dict:
    competitor = store.get_competitor(competitor_id, channel_id=channel_pk)
    if competitor is None:
        raise _not_found("competitor", competitor_id)
    return competitor


def _media_error_to_api(exc: Exception, filename: Optional[str]) -> ApiError:
    reason = (
        getattr(exc, "reason", None)
        or getattr(exc, "code", None)
        or "media_validation_failed"
    )
    return ApiError(
        422,
        str(reason),
        str(exc) or "The uploaded file failed media validation.",
        details={"reason": str(reason), "filename": filename},
    )


# ---------------------------------------------------------------------------
# Competitors
# ---------------------------------------------------------------------------


def add_competitor(
    channel_id: str,
    raw_input: str,
    display_name: Optional[str] = None,
) -> dict:
    """Parse + store a competitor for owner ``channel_id``.

    404 when the owner channel does not exist; 422
    ``invalid_competitor_input`` (with the parser's machine-readable
    ``reason``) when the input is not a YouTube channel/handle/id.
    Duplicate active competitors for the same channel are deduped by
    the store (the existing row is returned).
    """
    channel_pk = resolve_channel_id(channel_id)
    try:
        parsed = parse_competitor_input(raw_input)
    except StyleLabInputError as exc:
        raise ApiError(
            422,
            "invalid_competitor_input",
            str(exc),
            details={"reason": getattr(exc, "reason", "invalid_input"),
                     "field": "input"},
        ) from exc
    return _store().create_competitor(channel_pk, parsed, display_name=display_name)


def list_competitors(channel_id: str, *, include_archived: bool = False) -> list[dict]:
    channel_pk = resolve_channel_id(channel_id)
    return _store().list_competitors(channel_pk, include_archived=include_archived)


def get_competitor(channel_id: str, competitor_id: str) -> dict:
    channel_pk = resolve_channel_id(channel_id)
    return _require_competitor(_store(), channel_pk, competitor_id)


def archive_competitor(channel_id: str, competitor_id: str) -> dict:
    channel_pk = resolve_channel_id(channel_id)
    try:
        result = _store().archive_competitor(competitor_id, channel_id=channel_pk)
    except StyleLabScopeError:
        raise _not_found("competitor", competitor_id) from None
    if result is None:
        raise _not_found("competitor", competitor_id)
    return result


# ---------------------------------------------------------------------------
# Competitor videos — metadata attach (url_metadata / pasted_metadata)
# ---------------------------------------------------------------------------


def list_videos(channel_id: str, competitor_id: str) -> list[dict]:
    channel_pk = resolve_channel_id(channel_id)
    store = _store()
    _require_competitor(store, channel_pk, competitor_id)
    return store.list_videos(competitor_id, channel_id=channel_pk)


def attach_video_metadata(channel_id: str, competitor_id: str, payload: dict) -> dict:
    """Attach a video described by metadata only (no file).

    This is the first-class fallback path: a video known by URL or by
    pasted title/description/tags is stored as-is and is NEVER blocked
    on a metadata fetch.  ``source`` must be ``url_metadata`` or
    ``pasted_metadata`` (files go through :func:`attach_video_file`).
    """
    channel_pk = resolve_channel_id(channel_id)
    store = _store()
    _require_competitor(store, channel_pk, competitor_id)

    source = payload.get("source") or "pasted_metadata"
    if source not in ("url_metadata", "pasted_metadata"):
        raise ApiError(
            422,
            "validation_error",
            "source must be 'url_metadata' or 'pasted_metadata' for a "
            "metadata attach ('attached_file' uploads use the attach "
            "endpoint).",
            details={"field": "source", "value": source},
        )
    video_url = payload.get("video_url") or payload.get("url")
    external_video_id = payload.get("external_video_id") or payload.get("video_id")
    if not external_video_id and video_url:
        try:
            from app.style_lab.metadata import extract_video_id

            external_video_id = extract_video_id(video_url)
        except Exception:  # noqa: BLE001
            external_video_id = None
    title = payload.get("title")
    if not (title and str(title).strip()) and not video_url and not external_video_id:
        raise ApiError(
            422,
            "validation_error",
            "A metadata attach needs at least a title, a video_url or "
            "an external_video_id.",
            details={"field": "title"},
        )
    tags = payload.get("tags")
    fields: dict[str, Any] = {
        "source": source,
        "external_video_id": external_video_id,
        "video_url": video_url,
        "title": title,
        "description": payload.get("description"),
        "tags": list(tags) if tags is not None else None,
    }
    try:
        return store.add_video(competitor_id, fields, channel_id=channel_pk)
    except StyleLabScopeError:
        raise _not_found("competitor", competitor_id) from None
    except StyleLabNotFoundError:
        raise _not_found("competitor", competitor_id) from None
    except ValueError as exc:
        raise ApiError(
            422, "validation_error", str(exc), details={"field": "source"}
        ) from exc


# ---------------------------------------------------------------------------
# Competitor videos — file attach (streaming upload)
# ---------------------------------------------------------------------------


def _sanitize_filename(filename: Optional[str], fallback: str) -> str:
    name = Path(str(filename or "")).name.strip() or fallback
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name or fallback


async def _stream_to_temp(upload: Any, temp_path: Path, max_bytes: int, kind: str) -> tuple[int, str]:
    """Stream an UploadFile-like object to ``temp_path`` in chunks.

    Returns ``(size_bytes, sha256_hex)``.  The whole file is never in
    memory.  Raises ApiError 413 ``too_large`` past ``max_bytes`` (the
    media module enforces the same cap again while storing).
    """
    size = 0
    digest = hashlib.sha256()
    with open(temp_path, "wb") as out:
        while True:
            chunk = await upload.read(_CHUNK)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode()
            size += len(chunk)
            if size > max_bytes:
                raise ApiError(
                    413,
                    "too_large",
                    f"The {kind} file exceeds the {max_bytes}-byte limit.",
                    details={"reason": "too_large", "kind": kind,
                             "max_bytes": max_bytes},
                )
            digest.update(chunk)
            out.write(chunk)
    return size, digest.hexdigest()


async def attach_video_file(
    channel_id: str,
    competitor_id: str,
    video_upload: Any,
    *,
    thumbnail_upload: Any = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Attach a video FILE to a competitor (multipart upload path).

    The upload is streamed to a spool file in chunks (never whole in
    memory), then handed to :mod:`app.style_analysis.media`
    (``store_video_file`` / ``store_thumbnail``), which validates by
    probed content and stores content-addressed; the probed facts
    (duration / dimensions / mime / sha256 / size / storage paths) are
    recorded on a ``competitor_videos`` row with
    ``source='attached_file'``.  An invalid file fails with a 422
    ApiError carrying the media module's machine-readable ``reason``
    — and leaves NO row and NO stored file behind.
    """
    channel_pk = resolve_channel_id(channel_id)
    store = _store()
    _require_competitor(store, channel_pk, competitor_id)

    filename = _sanitize_filename(
        getattr(video_upload, "filename", None), "video.mp4"
    )
    data_dir = _data_dir()
    spool_dir = data_dir / "style_lab" / "tmp"
    spool_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    video_spool = spool_dir / f"{token}-video{Path(filename).suffix.lower()}"
    thumb_spool: Optional[Path] = None
    thumb_name: Optional[str] = None
    stored_paths: list[str] = []
    try:
        await _stream_to_temp(video_upload, video_spool, MAX_VIDEO_BYTES, "video")
        if thumbnail_upload is not None:
            thumb_name = _sanitize_filename(
                getattr(thumbnail_upload, "filename", None), "thumbnail.jpg"
            )
            thumb_spool = spool_dir / f"{token}-thumb{Path(thumb_name).suffix.lower()}"
            await _stream_to_temp(
                thumbnail_upload, thumb_spool, MAX_THUMBNAIL_BYTES, "thumbnail"
            )

        media = _media()
        if media is not None:
            fields = await _attach_via_media(
                media, channel_pk, competitor_id, filename,
                video_spool, thumb_spool, thumb_name, data_dir,
                title, description, stored_paths,
            )
        else:  # pragma: no cover - media module missing (parallel build)
            fields = _attach_via_fallback(
                channel_pk, competitor_id, filename,
                video_spool, thumb_spool, thumb_name,
                title, description, stored_paths,
            )
        try:
            return store.add_video(competitor_id, fields, channel_id=channel_pk)
        except StyleLabScopeError:
            raise _not_found("competitor", competitor_id) from None
        except StyleLabNotFoundError:
            raise _not_found("competitor", competitor_id) from None
    except BaseException:
        # A failed attach leaves no stored file behind.
        for rel in stored_paths:
            try:
                (data_dir / rel).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        for spool in (video_spool, thumb_spool):
            if spool is not None:
                try:
                    spool.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass


async def _attach_via_media(
    media: Any,
    channel_pk: str,
    competitor_id: str,
    filename: str,
    video_spool: Path,
    thumb_spool: Optional[Path],
    thumb_name: Optional[str],
    data_dir: Path,
    title: Optional[str],
    description: Optional[str],
    stored_paths: list[str],
) -> dict:
    """Validate + store via app.style_analysis.media (sync module — its
    blocking ffprobe/copy work runs in a worker thread)."""
    try:
        stored = await asyncio.to_thread(
            media.store_video_file,
            channel_pk,
            competitor_id,
            str(video_spool),
            filename,
            data_dir,
        )
    except MediaValidationError as exc:
        raise _media_error_to_api(exc, filename) from exc
    except Exception as exc:  # noqa: BLE001 - sibling error types
        raise _media_error_to_api(exc, filename) from exc
    stored_paths.append(stored.storage_path)
    info = stored.media_info

    thumb_path: Optional[str] = None
    thumb_sha: Optional[str] = None
    if thumb_spool is not None:
        try:
            stored_thumb = await asyncio.to_thread(
                media.store_thumbnail,
                channel_pk,
                competitor_id,
                str(thumb_spool),
                data_dir,
                original_filename=thumb_name,
            )
        except MediaValidationError as exc:
            raise _media_error_to_api(exc, thumb_name) from exc
        except Exception as exc:  # noqa: BLE001
            raise _media_error_to_api(exc, thumb_name) from exc
        stored_paths.append(stored_thumb.storage_path)
        thumb_path = stored_thumb.storage_path
        thumb_sha = stored_thumb.sha256

    return {
        "source": "attached_file",
        "title": title or Path(filename).stem,
        "description": description,
        "storage_path": stored.storage_path,
        "file_sha256": stored.sha256,
        "file_size_bytes": stored.size_bytes,
        "mime_type": info.mime_type,
        "duration_s": info.duration_s,
        "width": info.width,
        "height": info.height,
        "thumbnail_storage_path": thumb_path,
        "thumbnail_sha256": thumb_sha,
    }


def _attach_via_fallback(
    channel_pk: str,
    competitor_id: str,
    filename: str,
    video_spool: Path,
    thumb_spool: Optional[Path],
    thumb_name: Optional[str],
    title: Optional[str],
    description: Optional[str],
    stored_paths: list[str],
) -> dict:
    """Local probe + store used ONLY when the media module is absent
    (parallel-build tolerance).  Same contract: probed validation,
    machine-readable reasons, data-dir-relative storage paths."""
    try:
        info = _probe_video_local(video_spool, filename)
    except MediaValidationError as exc:
        raise _media_error_to_api(exc, filename) from exc
    size = video_spool.stat().st_size
    digest = hashlib.sha256(video_spool.read_bytes()).hexdigest()
    dest_rel = (
        Path("style_lab") / "competitors" / str(competitor_id)
        / f"{uuid.uuid4().hex}-{filename}"
    )
    dest_abs = _data_dir() / dest_rel
    dest_abs.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(video_spool), str(dest_abs))
    stored_paths.append(str(dest_rel))

    thumb_rel: Optional[str] = None
    thumb_sha: Optional[str] = None
    if thumb_spool is not None and thumb_name is not None:
        try:
            thumb_sha = _probe_thumbnail_local(thumb_spool, thumb_name)
        except MediaValidationError as exc:
            raise _media_error_to_api(exc, thumb_name) from exc
        thumb_dest_rel = (
            Path("style_lab") / "competitors" / str(competitor_id)
            / f"{uuid.uuid4().hex}-thumb-{thumb_name}"
        )
        thumb_abs = _data_dir() / thumb_dest_rel
        thumb_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(thumb_spool), str(thumb_abs))
        stored_paths.append(str(thumb_dest_rel))
        thumb_rel = str(thumb_dest_rel)

    return {
        "source": "attached_file",
        "title": title or Path(filename).stem,
        "description": description,
        "storage_path": str(dest_rel),
        "file_sha256": digest,
        "file_size_bytes": size,
        "mime_type": info.get("mime_type"),
        "duration_s": info.get("duration_s"),
        "width": info.get("width"),
        "height": info.get("height"),
        "thumbnail_storage_path": thumb_rel,
        "thumbnail_sha256": thumb_sha,
    }


def _probe_video_local(path: Path, filename: str) -> dict:
    """Local ffprobe fallback probe (media-module contract)."""
    ext = Path(filename).suffix.lower()
    if ext not in _VIDEO_EXTS:
        raise MediaValidationError(
            "unsupported_format",
            f"Unsupported video file type '{ext or filename}'.",
        )
    try:
        proc = subprocess.run(
            [ffprobe_executable(), "-v", "error", "-show_streams", "-show_format",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        raise MediaValidationError(
            "ffprobe_failed", f"Could not probe the uploaded file: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise MediaValidationError(
            "ffprobe_failed", "The uploaded file is not a readable media file."
        )
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise MediaValidationError(
            "ffprobe_failed", "ffprobe returned an unreadable result."
        ) from exc
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaValidationError(
            "no_video_stream", "The uploaded file contains no video stream."
        )
    fmt = payload.get("format") or {}
    duration = fmt.get("duration") or video.get("duration")
    duration_f = float(duration) if duration is not None else None
    if duration_f is None or duration_f <= 0:
        raise MediaValidationError(
            "zero_duration", "The uploaded file has no playable duration."
        )
    return {
        "duration_s": duration_f,
        "width": int(video["width"]) if video.get("width") else None,
        "height": int(video["height"]) if video.get("height") else None,
        "mime_type": _EXT_MIME.get(ext, "application/octet-stream"),
    }


def _probe_thumbnail_local(path: Path, filename: str) -> str:
    """Local thumbnail validation fallback; returns the sha256 hex."""
    ext = Path(filename).suffix.lower()
    if ext not in _IMAGE_EXTS:
        raise MediaValidationError(
            "unsupported_format",
            f"Unsupported thumbnail file type '{ext or filename}'.",
        )
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.verify()
    except Exception as exc:  # noqa: BLE001
        raise MediaValidationError(
            "not_an_image", "The thumbnail file is not a valid image."
        ) from exc
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Metadata fetch
# ---------------------------------------------------------------------------


def _persist_metadata(competitor_id: str, channel_pk: str, result: Any) -> dict:
    """Persist a metadata-fetch outcome on the competitor row (scoped)."""
    status = getattr(result, "status", None) or "fetch_failed"
    updates: dict[str, Any] = {"metadata_status": status, "updated_at": _now_ms()}
    if status == "fetched":
        metadata_payload: dict[str, Any] = {}
        raw_json = getattr(result, "raw_json", None)
        if isinstance(raw_json, dict):
            metadata_payload["api"] = raw_json
        for key in ("subscriber_count", "view_count", "video_count"):
            value = getattr(result, key, None)
            if value is not None:
                metadata_payload[key] = value
        if getattr(result, "video_id", None):
            metadata_payload["video"] = {
                "id": result.video_id,
                "title": getattr(result, "title", None),
                "description": getattr(result, "description", None),
                "tags": getattr(result, "tags", None) or [],
            }
        updates["metadata_json"] = json.dumps(metadata_payload)
        if getattr(result, "display_name", None):
            updates["display_name"] = result.display_name
        if getattr(result, "external_channel_id", None):
            updates["external_channel_id"] = result.external_channel_id
        if getattr(result, "handle", None):
            updates["handle"] = result.handle
        if getattr(result, "channel_url", None):
            updates["channel_url"] = result.channel_url
    else:
        updates["metadata_json"] = json.dumps(
            {
                "status": status,
                "reason": getattr(result, "reason", None),
                "detail": getattr(result, "detail", None) or "",
            }
        )
    conn = _new_conn()
    try:
        sets = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE competitor_channels SET {sets}"
            " WHERE id = ? AND channel_id = ?",
            (*updates.values(), str(competitor_id), str(channel_pk)),
        )
        try:
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        _close(conn)
    fresh = _store().get_competitor(competitor_id, channel_id=channel_pk)
    if fresh is None:  # pragma: no cover - row was verified above
        raise _not_found("competitor", competitor_id)
    fresh["fetch_status"] = status
    fresh["fetch_reason"] = getattr(result, "reason", None)
    return fresh


async def fetch_metadata(channel_id: str, competitor_id: str) -> dict:
    """Fetch (or honestly report the unavailability of) public metadata
    for one competitor and persist the outcome.

    Delegates to ``app.style_lab.metadata.fetch_channel_metadata`` —
    with no usable credential that returns ``metadata_unavailable``
    and makes NO HTTP request.  A competitor created from a bare video
    URL passes its ``source_input`` as the video URL, so the fetch can
    resolve the owning channel from the video's own metadata.
    """
    channel_pk = resolve_channel_id(channel_id)
    store = _store()
    competitor = _require_competitor(store, channel_pk, competitor_id)

    from app.style_lab import metadata as metadata_module

    video_url = (
        competitor.get("source_input")
        if competitor.get("source_kind") == "video_url"
        else None
    )
    result = await metadata_module.fetch_channel_metadata(
        channel_id=competitor.get("external_channel_id"),
        handle=competitor.get("handle"),
        channel_url=competitor.get("channel_url"),
        video_url=video_url,
    )
    return _persist_metadata(competitor_id, channel_pk, result)


# ---------------------------------------------------------------------------
# Analysis seam
# ---------------------------------------------------------------------------


def request_analysis(channel_id: str, video_id: str) -> dict:
    """Queue one competitor video for analysis — THE ANALYSIS SEAM.

    The video must belong to a competitor owned by ``channel_id``
    (cross-channel -> 404).  Sets ``analysis_status='queued'`` via the
    store; the analysis engine claims queued rows from here (see the
    module docstring).
    """
    channel_pk = resolve_channel_id(channel_id)
    try:
        result = _store().set_analysis_status(
            video_id, "queued", channel_id=channel_pk
        )
    except StyleLabScopeError:
        raise _not_found("video", video_id) from None
    if result is None:
        raise _not_found("video", video_id)
    return result


# ---------------------------------------------------------------------------
# Style profiles
# ---------------------------------------------------------------------------


def _invalid_style_profile(message: str, *, reason: str,
                           errors: Any = None) -> ApiError:
    """422 for a profile payload that fails the v1 schema / scoping."""
    details: dict[str, Any] = {"reason": reason, "field": "profile"}
    if errors is not None:
        details["errors"] = errors
    return ApiError(422, "invalid_style_profile", message, details=details)


def _validate_style_profile_payload(
    profile: Any, *, channel_pk: str, raw_channel_id: str
) -> StyleProfile:
    """Validate ``profile`` against the strict ``style_profile.v1``
    schema — the exact :class:`StyleProfile` model the synthesis layer
    (:mod:`app.integration.style_profile`) uses.  No second validator:
    this imports and reuses that model as-is.

    Accepts a dict or a JSON string.  Raises 422
    ``invalid_style_profile`` on any schema violation, and also when
    the profile's own ``channel_id`` names a different channel than
    the owner channel the profile is being created under (a profile
    is channel-scoped; either the internal id or the channel
    reference used in the request path is accepted).
    """
    payload = profile
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise _invalid_style_profile(
                "The style profile is not valid JSON.",
                reason="profile_not_valid_json",
            ) from exc
    if not isinstance(payload, dict):
        raise _invalid_style_profile(
            "The style profile must be a style_profile.v1 JSON object.",
            reason="profile_not_an_object",
        )
    try:
        validated = StyleProfile.model_validate(payload)
    except ValidationError as exc:
        errors = [
            {"type": err.get("type"), "loc": [str(p) for p in err.get("loc", ())],
             "msg": err.get("msg")}
            for err in exc.errors()
        ]
        raise _invalid_style_profile(
            "The style profile does not match the style_profile.v1 "
            "schema.",
            reason="schema_validation_failed",
            errors=errors,
        ) from exc
    if validated.channel_id not in (str(channel_pk), str(raw_channel_id)):
        raise _invalid_style_profile(
            "The style profile's channel_id does not match the "
            "channel it is being created for.",
            reason="channel_mismatch",
        )
    return validated


def _require_competitors_in_channel(
    store: StyleLabStore, channel_pk: str, competitor_ids: list[str]
) -> None:
    """Pre-persistence scope check with the store's own error mapping.

    Unknown id -> 422 ``competitor_not_found``; another channel's
    competitor -> 422 ``competitor_not_in_channel`` (the same codes
    :func:`create_profile` has always returned via the store).  Runs
    BEFORE any write so a refused create persists nothing; the store
    re-checks atomically inside its own transaction as well.
    """
    for cid in competitor_ids:
        competitor = store.get_competitor(cid)
        if competitor is None:
            raise ApiError(
                422,
                "competitor_not_found",
                f"competitor {cid} does not exist",
                details={"reason": "competitor_not_found"},
            )
        if str(competitor["channel_id"]) != str(channel_pk):
            raise ApiError(
                422,
                "competitor_not_in_channel",
                "A style profile can only reference competitors owned "
                "by this channel.",
                details={"reason": "competitor_not_in_channel"},
            )


def _competitor_source_analyses(
    store: StyleLabStore, channel_pk: str, competitor_ids: list[str]
) -> list[CompetitorVideoAnalysis]:
    """Build the no-copy source set for ``competitor_ids``.

    One :class:`CompetitorVideoAnalysis` per stored competitor video
    carrying its title / description — exactly the inputs
    :func:`assert_no_copy` checks — plus any fetched video metadata
    (title / description) persisted on the competitor row.  (The
    Style Lab DB stores no transcript text; ``assert_no_copy``'s own
    contract is titles/descriptions, so this is its full input set.)
    Asset ids / sha256 fall back to inert placeholders when a
    metadata-only video has none — identifiers are not content.
    """
    analyses: list[CompetitorVideoAnalysis] = []
    for cid in competitor_ids:
        for video in store.list_videos(cid, channel_id=channel_pk):
            title = video.get("title") or None
            description = video.get("description") or None
            if not (title and str(title).strip()) \
                    and not (description and str(description).strip()):
                continue
            try:
                duration = float(video.get("duration_s") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            analyses.append(CompetitorVideoAnalysis(
                source={
                    "kind": str(video.get("source") or "pasted_metadata"),
                    "asset_id": str(video.get("id")),
                    "sha256": str(video.get("file_sha256") or "0" * 64),
                },
                duration_s=duration if duration > 0 else 1.0,
                seo={"title_text": title, "description_text": description},
            ))
        competitor = store.get_competitor(cid, channel_id=channel_pk)
        metadata = competitor.get("metadata") if competitor else None
        if isinstance(metadata, dict):
            candidates: list[tuple[Any, Any]] = []
            video_meta = metadata.get("video")
            if isinstance(video_meta, dict):
                candidates.append(
                    (video_meta.get("title"), video_meta.get("description")))
            candidates.append(
                (metadata.get("title"), metadata.get("description")))
            for title, description in candidates:
                if not (title and str(title).strip()) \
                        and not (description and str(description).strip()):
                    continue
                analyses.append(CompetitorVideoAnalysis(
                    source={
                        "kind": "competitor_metadata",
                        "asset_id": f"{cid}-metadata",
                        "sha256": "0" * 64,
                    },
                    duration_s=1.0,
                    seo={"title_text": title or None,
                         "description_text": description or None},
                ))
    return analyses


def _enforce_no_copy(validated: StyleProfile,
                     analyses: list[CompetitorVideoAnalysis]) -> None:
    """Run the synthesis layer's exact :func:`assert_no_copy` gate."""
    try:
        assert_no_copy(validated, analyses)
    except NoCopyViolationError as exc:
        raise ApiError(
            422,
            "no_copy_violation",
            str(exc),
            details={"reason": "no_copy_violation"},
        ) from exc


def _gate_profile_content(
    store: StyleLabStore,
    channel_pk: str,
    raw_channel_id: str,
    competitor_ids: list[str],
    profile: Any,
) -> StyleProfile:
    """Schema + no-copy gate for profile CONTENT (pre-persistence)."""
    validated = _validate_style_profile_payload(
        profile, channel_pk=channel_pk, raw_channel_id=raw_channel_id
    )
    analyses = _competitor_source_analyses(store, channel_pk, competitor_ids)
    _enforce_no_copy(validated, analyses)
    return validated


def create_profile(
    channel_id: str,
    *,
    name: str,
    competitor_ids: Optional[list] = None,
    profile: Any = None,
    version: Optional[int] = None,
    activate: bool = False,
) -> dict:
    """Create a style profile for ``channel_id`` (delegates to store).

    Every competitor id must belong to this channel: an unknown id or
    another channel's competitor is a 422 (the create is refused, so
    a profile can never span channels).  An optional ``profile``
    payload / explicit ``version`` — which the store's create does
    not carry — is persisted by this service in the same scoped
    fashion as metadata results.

    SL-001 gate: a supplied ``profile`` payload is validated against
    the strict ``style_profile.v1`` schema and run through
    ``assert_no_copy`` against the listed competitors' stored titles
    / descriptions BEFORE the store is touched, so a rejected create
    (422 ``invalid_style_profile`` / ``no_copy_violation`` /
    competitor-scope 422) persists NOTHING — no partial row.  What
    is persisted is the validated model's canonical JSON, never the
    raw caller payload.  A create with no ``profile`` payload
    persists no profile content and is not gated on content.
    """
    channel_pk = resolve_channel_id(channel_id)
    clean_name = (name or "").strip()
    if not clean_name:
        raise ApiError(
            422, "validation_error", "A style profile needs a name.",
            details={"field": "name"},
        )
    store = _store()
    clean_competitor_ids = [str(c) for c in (competitor_ids or [])]
    # Scope first, content second — both BEFORE any persistence.
    _require_competitors_in_channel(store, channel_pk, clean_competitor_ids)
    validated: Optional[StyleProfile] = None
    if profile is not None:
        validated = _gate_profile_content(
            store, channel_pk, channel_id, clean_competitor_ids, profile
        )
    try:
        row = store.create_profile(
            channel_pk, clean_name, clean_competitor_ids
        )
    except StyleLabScopeError as exc:
        raise ApiError(
            422,
            "competitor_not_in_channel",
            "A style profile can only reference competitors owned by "
            "this channel.",
            details={"reason": "competitor_not_in_channel",
                     "detail": str(exc)},
        ) from exc
    except StyleLabNotFoundError as exc:
        raise ApiError(
            422,
            "competitor_not_found",
            str(exc),
            details={"reason": "competitor_not_found"},
        ) from exc

    if validated is not None or version is not None:
        updates: dict[str, Any] = {"updated_at": _now_ms()}
        if validated is not None:
            updates["profile_json"] = json.dumps(
                validated.model_dump(mode="json")
            )
        if version is not None:
            updates["version"] = int(version)
        conn = _new_conn()
        try:
            sets = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE style_profiles SET {sets}"
                " WHERE id = ? AND channel_id = ?",
                (*updates.values(), row["id"], channel_pk),
            )
            try:
                conn.commit()
            except Exception:  # noqa: BLE001
                pass
        finally:
            _close(conn)
        refreshed = store.get_profile(row["id"], channel_id=channel_pk)
        if refreshed is not None:
            row = refreshed

    if activate:
        activated = store.activate_profile(row["id"], channel_id=channel_pk)
        if activated is not None:
            row = activated
    return row


def list_profiles(channel_id: str) -> list[dict]:
    channel_pk = resolve_channel_id(channel_id)
    return _store().list_profiles(channel_pk)


def activate_profile(channel_id: str, profile_id: str) -> dict:
    """Activate a profile; the channel's previously active profile is
    superseded by the store in the same transaction.

    Activation persists no profile content of its own (it only flips
    ``status``), but as defence-in-depth for rows written before the
    SL-001 creation gate existed, a stored content-bearing profile is
    re-checked (schema + ``assert_no_copy`` against its own
    competitors' stored source material) before it may go active; a
    violating legacy row is refused with a 422 and stays non-active.
    Content-free rows (``profile_json`` NULL) activate as before.
    """
    channel_pk = resolve_channel_id(channel_id)
    store = _store()
    existing = store.get_profile(profile_id, channel_id=channel_pk)
    if existing is None:
        raise _not_found("profile", profile_id)
    raw_content = existing.get("profile_json")
    if raw_content is not None:
        stored_ids = [str(c) for c in (existing.get("competitor_ids") or [])]
        _gate_profile_content(
            store, channel_pk, channel_id, stored_ids, raw_content
        )
    try:
        result = store.activate_profile(profile_id, channel_id=channel_pk)
    except StyleLabScopeError:
        raise _not_found("profile", profile_id) from None
    if result is None:
        raise _not_found("profile", profile_id)
    return result
