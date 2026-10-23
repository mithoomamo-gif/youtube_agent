"""Reporting service — the W1/W2 seam for Agent 31 reports (Round 5).

SERVICE-LAYER API (the contract W2's HTTP endpoints + weekly digest
build on — W2 owns the routers and the digest variant; this module
owns generation, rendering and storage):

    from app.reporting import service  # or: from app.reporting.service import ...

    # Generate + persist (returns the full report dict — the JSON model)
    report = service.generate_video_report(source, project_id)
    report = service.generate_channel_report(
        source, channel_id,
        period_start_ms=..., period_end_ms=..., period_label="last_28_days",
    )

    # Build WITHOUT persisting (pure ledger read; same dict shape)
    report = service.build_video_report(source, project_id)
    report = service.build_channel_report(source, channel_id)

    # List stored reports (metadata only, newest first)
    metas = service.list_reports(source, channel_id=..., scope="video", limit=50)

    # Fetch one stored report
    full = service.get_report(source, report_id)
    #   -> {"metadata": {...}, "report": {...}, "markdown": str} or None
    md = service.get_report_markdown(source, report_id)   # -> str or None

    # Render Markdown from any report dict (JSON is the source)
    md = service.render_report_markdown(report)

``source`` is anything :func:`app.reporting.store.open_conn`
accepts: a ``sqlite3.Connection``, a conn_factory callable, a DB
path (``str`` / ``Path``), or ``None`` for the app's configured
default database.  Connections passed in are caller-owned and never
closed; connections the service opens are always closed.

Generation semantics:

* Reports are built ONLY from ledgers (see
  :mod:`app.reporting.engine` for the section → ledger map) and are
  persisted as artefacts in the ``generated_reports`` table (full
  JSON + rendered Markdown inline) so the UI can list them via
  :func:`list_reports`.  When a reports directory is resolvable
  (explicit ``reports_dir``, else ``<data_dir>/reports`` from
  ``YTA_DATA_DIR`` / app settings), ``<report_id>.json`` and
  ``<report_id>.md`` file copies are also written and their paths
  recorded on the row; a file-write failure never loses the report
  (the DB row is complete on its own).
* ``persist=False`` on the generate functions returns the built
  report without storing it (the digest / preview path).
* ``generate_*`` raises
  :class:`app.reporting.engine.ReportNotFoundError` for an unknown
  project / channel.  An existing project / channel with NO ledger
  rows generates successfully with honest empty sections.
* The weekly digest is a W2 variant built on these same functions —
  it is deliberately not implemented here.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.reporting import engine, store
from app.reporting.render import render_markdown

__all__ = [
    "generate_video_report",
    "generate_channel_report",
    "build_video_report",
    "build_channel_report",
    "list_reports",
    "get_report",
    "get_report_markdown",
    "render_report_markdown",
    "ReportNotFoundError",
]

ReportNotFoundError = engine.ReportNotFoundError

# Re-exported so W2 has one import surface for rendering.
render_report_markdown = render_markdown


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Build (no persistence)
# ---------------------------------------------------------------------------


def build_video_report(source: Any, project_id: str, **kwargs: Any) -> dict[str, Any]:
    """Build a per-video report dict from ledgers; does not persist."""
    conn, should_close = store.open_conn(source)
    try:
        return engine.build_video_report(conn, str(project_id), **kwargs)
    finally:
        store._close(conn, should_close)


def build_channel_report(source: Any, channel_id: str, **kwargs: Any) -> dict[str, Any]:
    """Build a per-channel report dict from ledgers; does not persist."""
    conn, should_close = store.open_conn(source)
    try:
        return engine.build_channel_report(conn, str(channel_id), **kwargs)
    finally:
        store._close(conn, should_close)


# ---------------------------------------------------------------------------
# Generate + persist
# ---------------------------------------------------------------------------


def _resolve_reports_dir(
    source: Any, reports_dir: Any
) -> Optional[Path]:
    if reports_dir is not None:
        return Path(reports_dir)
    if isinstance(source, (str, Path)):
        return Path(source).expanduser().parent / "reports"
    env_dir = os.environ.get("YTA_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "reports"
    try:
        from app.config import get_settings

        return Path(get_settings().data_dir) / "reports"
    except Exception:
        return None


def _persist(
    source: Any,
    report: dict[str, Any],
    *,
    reports_dir: Any = None,
) -> dict[str, Any]:
    markdown_text = render_markdown(report)
    json_path: Optional[str] = None
    markdown_path: Optional[str] = None
    directory = _resolve_reports_dir(source, reports_dir)
    if directory is not None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            json_file = directory / f"{report['report_id']}.json"
            md_file = directory / f"{report['report_id']}.md"
            json_file.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            md_file.write_text(markdown_text, encoding="utf-8")
            json_path = str(json_file)
            markdown_path = str(md_file)
        except Exception:
            # File store is best-effort (incl. the VM's intermittent
            # EPERM write fault): the DB artefact below is complete.
            json_path = None
            markdown_path = None
    store.save_report(
        source, report, markdown_text,
        json_path=json_path, markdown_path=markdown_path,
    )
    return report


def generate_video_report(
    source: Any,
    project_id: str,
    *,
    period_start_ms: Optional[int] = None,
    period_end_ms: Optional[int] = None,
    period_label: Optional[str] = None,
    persist: bool = True,
    reports_dir: Any = None,
    generated_at_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Generate (and by default persist) the per-video receipt.

    Returns the full report dict (the JSON model).  Raises
    :class:`ReportNotFoundError` for an unknown project.
    """
    report = build_video_report(
        source,
        project_id,
        report_id=uuid.uuid4().hex,
        generated_at_ms=(
            generated_at_ms if generated_at_ms is not None else _now_ms()
        ),
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        period_label=period_label,
    )
    if persist:
        _persist(source, report, reports_dir=reports_dir)
    return report


def generate_channel_report(
    source: Any,
    channel_id: str,
    *,
    period_start_ms: Optional[int] = None,
    period_end_ms: Optional[int] = None,
    period_label: Optional[str] = None,
    persist: bool = True,
    reports_dir: Any = None,
    generated_at_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Generate (and by default persist) the per-channel receipt.

    Returns the full report dict (the JSON model).  Raises
    :class:`ReportNotFoundError` for an unknown channel.
    """
    report = build_channel_report(
        source,
        channel_id,
        report_id=uuid.uuid4().hex,
        generated_at_ms=(
            generated_at_ms if generated_at_ms is not None else _now_ms()
        ),
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        period_label=period_label,
    )
    if persist:
        _persist(source, report, reports_dir=reports_dir)
    return report


# ---------------------------------------------------------------------------
# Read seam (list + fetch stored reports)
# ---------------------------------------------------------------------------


def list_reports(source: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Listing metadata for stored reports (see store.list_reports)."""
    return store.list_reports(source, **kwargs)


def get_report(source: Any, report_id: Any) -> Optional[dict[str, Any]]:
    """One stored report: ``{metadata, report, markdown}`` or None.

    ``report`` is the parsed JSON model; ``markdown`` is the stored
    rendering, re-rendered from the JSON if the stored copy is
    missing (rendering is a pure function of the JSON, so both are
    identical by construction).
    """
    row = store.get_report_row(source, report_id)
    if row is None:
        return None
    try:
        report = json.loads(row["report_json"])
    except (TypeError, ValueError):
        report = {}
    markdown_text = row.get("markdown_text") or render_markdown(report)
    return {
        "metadata": store.report_metadata(row),
        "report": report,
        "markdown": markdown_text,
    }


def get_report_markdown(source: Any, report_id: Any) -> Optional[str]:
    """The stored Markdown rendering for ``report_id``, or None."""
    fetched = get_report(source, report_id)
    return fetched["markdown"] if fetched is not None else None
