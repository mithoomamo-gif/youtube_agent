"""Style Lab media intake: probe, validate and store user-supplied files.

SCOPE / HARD BOUNDARY
---------------------
Style Lab analyses competitor videos that the USER supplies (attached /
uploaded files). Downloading from YouTube is OUT of scope: this module
contains no network code and no download path of any kind. It only
accepts a local path, raw bytes, or a readable byte stream.

STORAGE LAYOUT
--------------
::

    <data_dir>/style_lab/<owner_channel_id>/<competitor_id>/<sha256><ext>
    <data_dir>/style_lab/<owner_channel_id>/<competitor_id>/<sha256>-thumb<ext>

``data_dir`` is the app data dir from ``app.config`` (``YTA_DATA_DIR`` /
``settings.data_dir``; the DB lives at ``<data_dir>/app.db``). Callers
pass it explicitly so this module stays pure and testable. All returned
``storage_path`` values are RELATIVE to ``data_dir`` (POSIX separators)
so they can be persisted in the DB and resolved later against whatever
data dir the app is launched with.

GUARANTEES
----------
* Validation is by PROBED content (ffprobe format / Pillow image format),
  never by filename extension alone. A text file renamed ``.mp4`` is
  rejected.
* Copies are streamed in 1 MiB chunks while hashing — the whole file is
  never loaded into memory. Size caps are enforced DURING the copy, so
  an oversize stream is rejected without being fully written.
* Writes are atomic: bytes land in a temp file in the destination
  directory and are ``os.replace``-renamed into place only after the
  content validates. A rejected upload leaves no partial file behind.
* Dedupe: content is addressed by its SHA-256. Storing the same bytes
  again for the same competitor returns the existing path and writes no
  second copy.
* Path safety: owner/competitor ids are app-generated, but segments are
  still sanitised (anything outside ``[A-Za-z0-9._-]`` is replaced, dot
  segments are neutralised) and the final resolved path is verified to
  stay inside ``<data_dir>/style_lab`` before anything is written.

ERRORS
------
All validation failures raise :class:`MediaValidationError` carrying a
machine-readable ``reason`` (see the ``REASON_*`` constants):

``missing_file``        source path does not exist / is not a file
``empty_file``          source is zero bytes
``invalid_source``      source is neither a path, bytes, nor a stream
``ffprobe_failed``      ffprobe missing / non-zero exit / unparsable, or
                        the probed file is unreadable/corrupt
``no_video_stream``     probed file has no video stream
``zero_duration``       probed duration missing or <= 0
``unsupported_format``  probed container is not mp4/mov/mkv/webm/m4v
``too_large``           content exceeds the (injectable) size cap
``not_an_image``        thumbnail bytes are not a jpg/png/webp image
``invalid_path_segment`` an id sanitises down to nothing usable
``path_escape``         resolved destination would leave the storage root

The ffprobe invocation mirrors the codebase's existing probes
(``app.pipeline.render.qc.ffprobe`` / ``app.pipeline.qc.technical``):
``ffprobe -v error -show_streams -show_format -of json <path>`` via
``subprocess.run`` with an argv list (no shell).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Union

from ..ffmpeg_resolution import ffprobe_executable

__all__ = [
    "ALLOWED_VIDEO_EXTENSIONS",
    "ALLOWED_IMAGE_FORMATS",
    "MAX_THUMBNAIL_BYTES",
    "MAX_VIDEO_BYTES",
    "MediaInfo",
    "MediaValidationError",
    "REASON_EMPTY_FILE",
    "REASON_FFPROBE_FAILED",
    "REASON_INVALID_PATH_SEGMENT",
    "REASON_INVALID_SOURCE",
    "REASON_MISSING_FILE",
    "REASON_NOT_AN_IMAGE",
    "REASON_NO_VIDEO_STREAM",
    "REASON_PATH_ESCAPE",
    "REASON_TOO_LARGE",
    "REASON_UNSUPPORTED_FORMAT",
    "REASON_ZERO_DURATION",
    "StoredMedia",
    "StoredThumbnail",
    "probe_media",
    "store_thumbnail",
    "store_video_file",
]

# ---------------------------------------------------------------------------
# Caps and allowlists (documented contract)
# ---------------------------------------------------------------------------

#: Hard cap for one competitor video: 2 GiB. Enforced while streaming.
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024

#: Hard cap for one competitor thumbnail: 16 MiB. Enforced while streaming.
MAX_THUMBNAIL_BYTES = 16 * 1024 * 1024

#: Copy/hash chunk size — files are never held whole in memory.
_CHUNK_SIZE = 1024 * 1024

#: Filename extensions a stored video may carry (one is chosen from the
#: PROBED container family; see ``_VIDEO_FORMAT_FAMILIES``).
ALLOWED_VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")

#: Probed-container families -> (allowed extensions, canonical extension).
#: ffprobe cannot distinguish mp4 from mov (both report
#: ``mov,mp4,m4a,3gp,3g2,mj2``) nor mkv from webm (both report
#: ``matroska,webm``), so within a family the uploader's original
#: extension is kept when it belongs to the family, else the canonical
#: extension is used.
_VIDEO_FORMAT_FAMILIES: tuple[tuple[frozenset, tuple, str], ...] = (
    (frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}),
     (".mp4", ".mov", ".m4v"), ".mp4"),
    (frozenset({"matroska", "webm"}),
     (".mkv", ".webm"), ".mkv"),
)

_VIDEO_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}

#: Pillow image format -> (extension, mime). Anything else is rejected.
ALLOWED_IMAGE_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
}

#: Directory name (under ``data_dir``) that roots all Style Lab media.
STYLE_LAB_DIRNAME = "style_lab"

# ---------------------------------------------------------------------------
# Machine-readable rejection reasons
# ---------------------------------------------------------------------------

REASON_MISSING_FILE = "missing_file"
REASON_EMPTY_FILE = "empty_file"
REASON_INVALID_SOURCE = "invalid_source"
REASON_FFPROBE_FAILED = "ffprobe_failed"
REASON_NO_VIDEO_STREAM = "no_video_stream"
REASON_ZERO_DURATION = "zero_duration"
REASON_UNSUPPORTED_FORMAT = "unsupported_format"
REASON_TOO_LARGE = "too_large"
REASON_NOT_AN_IMAGE = "not_an_image"
REASON_INVALID_PATH_SEGMENT = "invalid_path_segment"
REASON_PATH_ESCAPE = "path_escape"


class MediaValidationError(ValueError):
    """A supplied media file failed validation.

    ``reason`` is the machine-readable code (one of the ``REASON_*``
    constants); ``str(exc)`` is the human-readable message. ``code`` is
    an alias of ``reason`` for callers that speak the API error envelope.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.code = reason
        self.message = message


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaInfo:
    """Probed facts about one media file."""

    duration_s: float
    width: int | None
    height: int | None
    mime_type: str
    size_bytes: int
    #: Raw ffprobe ``format_name`` (videos) or Pillow format (images).
    format_name: str = ""
    #: Video codec name when probed (None for images / unknown).
    video_codec: str | None = None


@dataclass(frozen=True)
class StoredMedia:
    """Result of storing one competitor video."""

    #: Path relative to ``data_dir`` (POSIX separators) — persist THIS.
    storage_path: str
    sha256: str
    size_bytes: int
    media_info: MediaInfo

    @property
    def info(self) -> MediaInfo:
        return self.media_info

    def resolve(self, data_dir: Union[str, Path]) -> Path:
        """Absolute path of the stored file under ``data_dir``."""
        return Path(data_dir) / self.storage_path


@dataclass(frozen=True)
class StoredThumbnail:
    """Result of storing one competitor thumbnail image."""

    #: Path relative to ``data_dir`` (POSIX separators) — persist THIS.
    storage_path: str
    sha256: str
    size_bytes: int
    media_info: MediaInfo

    @property
    def info(self) -> MediaInfo:
        return self.media_info

    def resolve(self, data_dir: Union[str, Path]) -> Path:
        """Absolute path of the stored file under ``data_dir``."""
        return Path(data_dir) / self.storage_path


#: What ``store_video_file`` / ``store_thumbnail`` accept as a source:
#: a filesystem path, raw bytes, a binary stream, or an iterable of
#: byte chunks (e.g. an async-upload spool drained by the caller).
MediaSource = Union[str, Path, bytes, bytearray, memoryview, BinaryIO,
                    Iterable[bytes]]

# ---------------------------------------------------------------------------
# ffprobe plumbing (mirrors app.pipeline.render.qc.ffprobe)
# ---------------------------------------------------------------------------


def _run_ffprobe(path: Path, ffprobe_bin: str) -> dict:
    """Run ffprobe (JSON, no shell) and return the parsed report."""
    cmd = [
        ffprobe_bin, "-v", "error",
        "-show_streams", "-show_format", "-of", "json", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise MediaValidationError(
            REASON_FFPROBE_FAILED, f"ffprobe binary not found: {ffprobe_bin}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaValidationError(
            REASON_FFPROBE_FAILED, f"ffprobe timed out probing {path}"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:300]
        raise MediaValidationError(
            REASON_FFPROBE_FAILED, f"ffprobe failed for {path}: {detail}"
        )
    try:
        report = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MediaValidationError(
            REASON_FFPROBE_FAILED,
            f"ffprobe returned unparsable output for {path}",
        ) from exc
    if not isinstance(report, dict):
        raise MediaValidationError(
            REASON_FFPROBE_FAILED,
            f"ffprobe returned unparsable output for {path}",
        )
    return report


def _parse_duration(raw: object) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value


def _video_family(format_name: str) -> tuple[tuple, str] | None:
    """Map a probed ffprobe ``format_name`` to (extensions, canonical)."""
    tokens = {t.strip().lower() for t in (format_name or "").split(",") if t.strip()}
    for family_tokens, extensions, canonical in _VIDEO_FORMAT_FAMILIES:
        if tokens & family_tokens:
            return extensions, canonical
    return None


def _info_from_report(report: dict, size_bytes: int) -> MediaInfo:
    """Build :class:`MediaInfo` from an ffprobe report, enforcing the
    video-stream and positive-duration invariants."""
    streams = report.get("streams") or []
    video = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise MediaValidationError(
            REASON_NO_VIDEO_STREAM, "probed file contains no video stream"
        )
    fmt = report.get("format") or {}
    format_name = str(fmt.get("format_name") or "")
    duration = _parse_duration(fmt.get("duration"))
    if duration is None:
        duration = _parse_duration(video.get("duration"))
    if duration is None or duration <= 0:
        raise MediaValidationError(
            REASON_ZERO_DURATION,
            f"probed file has no positive duration (got {duration!r})",
        )
    family = _video_family(format_name)
    if family is not None:
        mime_type = _VIDEO_MIME_BY_EXT[family[1]]
    else:
        mime_type = "application/octet-stream"
    width = video.get("width")
    height = video.get("height")
    return MediaInfo(
        duration_s=duration,
        width=int(width) if isinstance(width, (int, float)) else None,
        height=int(height) if isinstance(height, (int, float)) else None,
        mime_type=mime_type,
        size_bytes=size_bytes,
        format_name=format_name,
        video_codec=video.get("codec_name"),
    )


def probe_media(path: Union[str, Path], *, ffprobe_bin: str = ffprobe_executable()) -> MediaInfo:
    """Probe one media file with ffprobe and return its :class:`MediaInfo`.

    Raises :class:`MediaValidationError` with reason ``missing_file``,
    ``empty_file``, ``ffprobe_failed``, ``no_video_stream`` or
    ``zero_duration``. Container allowlisting is NOT done here (an AVI
    probes fine) — :func:`store_video_file` enforces it.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise MediaValidationError(
            REASON_MISSING_FILE, f"media file not found: {file_path}"
        )
    size_bytes = file_path.stat().st_size
    if size_bytes == 0:
        raise MediaValidationError(
            REASON_EMPTY_FILE, f"media file is empty: {file_path}"
        )
    report = _run_ffprobe(file_path, ffprobe_bin)
    return _info_from_report(report, size_bytes)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

_UNSAFE_SEGMENT_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitise_segment(value: object, *, kind: str) -> str:
    """Reduce an id to ONE safe path segment.

    Separators and other unsafe characters collapse to ``-``; leading /
    trailing dots are stripped so the result can never be ``.``/``..``
    or a hidden traversal fragment. Raises ``invalid_path_segment`` when
    nothing usable remains.
    """
    raw = "" if value is None else str(value)
    cleaned = _UNSAFE_SEGMENT_CHARS.sub("-", raw.strip())
    cleaned = cleaned.strip(".")
    if cleaned in ("", ".", "..") or "/" in cleaned or "\\" in cleaned:
        raise MediaValidationError(
            REASON_INVALID_PATH_SEGMENT,
            f"{kind} id {value!r} does not reduce to a safe path segment",
        )
    return cleaned[:128]


def _competitor_dir(data_dir: Union[str, Path], owner_channel_id: object,
                    competitor_id: object) -> tuple[Path, Path]:
    """Return ``(resolved_data_dir, competitor_dir)`` with the resolved
    competitor dir provably inside ``<data_dir>/style_lab``."""
    resolved_data_dir = Path(data_dir).expanduser().resolve()
    root = (resolved_data_dir / STYLE_LAB_DIRNAME).resolve()
    owner = _sanitise_segment(owner_channel_id, kind="owner_channel")
    competitor = _sanitise_segment(competitor_id, kind="competitor")
    dest = (root / owner / competitor).resolve()
    if dest != root and root not in dest.parents:
        raise MediaValidationError(
            REASON_PATH_ESCAPE,
            f"storage destination escapes the style_lab root: {dest}",
        )
    return resolved_data_dir, dest


def _relative_storage_path(resolved_data_dir: Path, final_path: Path) -> str:
    try:
        return final_path.relative_to(resolved_data_dir).as_posix()
    except ValueError as exc:  # pragma: no cover - guarded by _competitor_dir
        raise MediaValidationError(
            REASON_PATH_ESCAPE,
            f"stored file is outside the data dir: {final_path}",
        ) from exc


# ---------------------------------------------------------------------------
# Streaming copy + hash (never whole-file in memory)
# ---------------------------------------------------------------------------


def _iter_source_chunks(source: MediaSource) -> Iterable[bytes]:
    """Yield byte chunks from a bytes / stream / chunk-iterable source."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        for offset in range(0, len(data), _CHUNK_SIZE):
            yield data[offset:offset + _CHUNK_SIZE]
        return
    read = getattr(source, "read", None)
    if callable(read):
        while True:
            chunk = read(_CHUNK_SIZE)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            yield bytes(chunk)
        return
    if isinstance(source, Iterable):
        for chunk in source:  # type: ignore[union-attr]
            if not chunk:
                continue
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            yield bytes(chunk)
        return
    raise MediaValidationError(
        REASON_INVALID_SOURCE,
        f"unsupported media source type: {type(source).__name__}",
    )


def _spool_to_temp(source: MediaSource, dest_dir: Path, *,
                   max_bytes: int, kind: str) -> tuple[Path, str, int]:
    """Stream ``source`` into a temp file inside ``dest_dir`` while
    hashing. Returns ``(temp_path, sha256_hex, size_bytes)``.

    The caller owns the temp file (rename it into place or unlink it).
    The size cap is enforced DURING the copy: the moment the running
    total exceeds ``max_bytes`` the temp file is removed and
    ``too_large`` is raised.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".incoming-", suffix=".part",
                                    dir=str(dest_dir))
    tmp_path = Path(tmp_name)
    hasher = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            if isinstance(source, (str, Path)):
                src_path = Path(source)
                if not src_path.is_file():
                    raise MediaValidationError(
                        REASON_MISSING_FILE, f"media file not found: {src_path}"
                    )
                with src_path.open("rb") as inp:
                    while True:
                        chunk = inp.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise MediaValidationError(
                                REASON_TOO_LARGE,
                                f"{kind} exceeds the {max_bytes}-byte size cap",
                            )
                        hasher.update(chunk)
                        out.write(chunk)
            else:
                for chunk in _iter_source_chunks(source):
                    total += len(chunk)
                    if total > max_bytes:
                        raise MediaValidationError(
                            REASON_TOO_LARGE,
                            f"{kind} exceeds the {max_bytes}-byte size cap",
                        )
                    hasher.update(chunk)
                    out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    if total == 0:
        tmp_path.unlink(missing_ok=True)
        raise MediaValidationError(
            REASON_EMPTY_FILE, f"{kind} source contained no bytes"
        )
    return tmp_path, hasher.hexdigest(), total


def _finalise(tmp_path: Path, dest_dir: Path, filename: str,
              resolved_data_dir: Path) -> tuple[Path, bool]:
    """Atomically move ``tmp_path`` to ``dest_dir/filename``.

    Returns ``(final_path, reused)`` — when a file with that (hash-based)
    name already exists it is kept and the temp copy is discarded
    (dedupe: identical name == identical content hash).
    """
    final_path = dest_dir / filename
    resolved_final = final_path.resolve()
    root = (resolved_data_dir / STYLE_LAB_DIRNAME).resolve()
    if resolved_final != root and root not in resolved_final.parents:
        tmp_path.unlink(missing_ok=True)
        raise MediaValidationError(
            REASON_PATH_ESCAPE,
            f"storage destination escapes the style_lab root: {resolved_final}",
        )
    if final_path.exists():
        tmp_path.unlink(missing_ok=True)
        return final_path, True
    os.replace(tmp_path, final_path)
    return final_path, False


def _original_suffix(original_filename: object) -> str:
    if not original_filename:
        return ""
    return Path(str(original_filename)).suffix.lower()


# ---------------------------------------------------------------------------
# Public storage API
# ---------------------------------------------------------------------------


def store_video_file(
    owner_channel_id: object,
    competitor_id: object,
    source_path_or_bytes_stream: MediaSource,
    original_filename: object,
    data_dir: Union[str, Path],
    *,
    max_bytes: int = MAX_VIDEO_BYTES,
    ffprobe_bin: str = ffprobe_executable(),
) -> StoredMedia:
    """Validate and store one user-supplied competitor video.

    ``source_path_or_bytes_stream`` may be a path (``str``/``Path``),
    raw ``bytes``, a binary stream, or an iterable of byte chunks.
    ``original_filename`` is the uploader's filename — used ONLY to pick
    the extension within the probed container family, never trusted for
    validation and never used as a storage path.

    The probed container must be in the allowlist (mp4 / mov / mkv /
    webm / m4v), the duration must be positive, and the content must be
    at most ``max_bytes`` (default :data:`MAX_VIDEO_BYTES` = 2 GiB;
    injectable for tests). The file is stored content-addressed as
    ``style_lab/<owner>/<competitor>/<sha256><canonical ext>``; storing
    identical bytes again for the same competitor dedupes to the
    existing file.
    """
    resolved_data_dir, dest_dir = _competitor_dir(
        data_dir, owner_channel_id, competitor_id)
    tmp_path, sha256, size_bytes = _spool_to_temp(
        source_path_or_bytes_stream, dest_dir, max_bytes=max_bytes, kind="video")
    try:
        report = _run_ffprobe(tmp_path, ffprobe_bin)
        info = _info_from_report(report, size_bytes)
        fmt = report.get("format") or {}
        family = _video_family(str(fmt.get("format_name") or ""))
        if family is None:
            raise MediaValidationError(
                REASON_UNSUPPORTED_FORMAT,
                "probed container is not in the allowlist "
                "(mp4, mov, mkv, webm, m4v): "
                f"{fmt.get('format_name')!r}",
            )
        extensions, canonical_ext = family
        suffix = _original_suffix(original_filename)
        ext = suffix if suffix in extensions else canonical_ext
        # Re-derive the mime from the chosen extension (mp4 vs mov etc.).
        info = MediaInfo(
            duration_s=info.duration_s,
            width=info.width,
            height=info.height,
            mime_type=_VIDEO_MIME_BY_EXT[ext],
            size_bytes=info.size_bytes,
            format_name=info.format_name,
            video_codec=info.video_codec,
        )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    final_path, _reused = _finalise(
        tmp_path, dest_dir, f"{sha256}{ext}", resolved_data_dir)
    return StoredMedia(
        storage_path=_relative_storage_path(resolved_data_dir, final_path),
        sha256=sha256,
        size_bytes=size_bytes,
        media_info=info,
    )


def store_thumbnail(
    owner_channel_id: object,
    competitor_id: object,
    image_path_or_bytes: MediaSource,
    data_dir: Union[str, Path],
    *,
    original_filename: object = None,
    max_bytes: int = MAX_THUMBNAIL_BYTES,
) -> StoredThumbnail:
    """Validate and store one user-supplied competitor thumbnail.

    Accepts the same source types as :func:`store_video_file`. The
    content must actually BE an image — validated with Pillow (the
    codebase's image library), allowing only JPEG / PNG / WebP — and at
    most ``max_bytes`` (default :data:`MAX_THUMBNAIL_BYTES` = 16 MiB;
    injectable for tests). Stored content-addressed as
    ``style_lab/<owner>/<competitor>/<sha256>-thumb<ext>`` with the same
    dedupe semantics as videos.
    """
    # Imported lazily so video-only callers never pay the Pillow import,
    # matching how the thumbnail pipeline treats it as a hard dependency
    # only on image paths.
    from PIL import Image, UnidentifiedImageError

    resolved_data_dir, dest_dir = _competitor_dir(
        data_dir, owner_channel_id, competitor_id)
    tmp_path, sha256, size_bytes = _spool_to_temp(
        image_path_or_bytes, dest_dir, max_bytes=max_bytes, kind="thumbnail")
    try:
        try:
            with Image.open(tmp_path) as img:
                img.load()
                pillow_format = (img.format or "").upper()
                width, height = img.size
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise MediaValidationError(
                REASON_NOT_AN_IMAGE,
                f"supplied thumbnail is not a readable image: {exc}",
            ) from exc
        if pillow_format not in ALLOWED_IMAGE_FORMATS:
            raise MediaValidationError(
                REASON_NOT_AN_IMAGE,
                "thumbnail image format is not in the allowlist "
                f"(jpg, png, webp): {pillow_format!r}",
            )
        if width <= 0 or height <= 0:
            raise MediaValidationError(
                REASON_NOT_AN_IMAGE,
                f"thumbnail image has no area: {width}x{height}",
            )
        ext, mime_type = ALLOWED_IMAGE_FORMATS[pillow_format]
        info = MediaInfo(
            duration_s=0.0,
            width=int(width),
            height=int(height),
            mime_type=mime_type,
            size_bytes=size_bytes,
            format_name=pillow_format.lower(),
            video_codec=None,
        )
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    final_path, _reused = _finalise(
        tmp_path, dest_dir, f"{sha256}-thumb{ext}", resolved_data_dir)
    return StoredThumbnail(
        storage_path=_relative_storage_path(resolved_data_dir, final_path),
        sha256=sha256,
        size_bytes=size_bytes,
        media_info=info,
    )
