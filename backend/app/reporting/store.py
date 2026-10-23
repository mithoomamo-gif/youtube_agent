"""Persistence for generated reports — DB store + connection plumbing.

The store half of the Reporting engine, following the codebase
patterns of :mod:`app.learning.store` / :mod:`app.style_lab.store`:

* constructed around a *source* that may be a ``sqlite3.Connection``
  (shared, never closed), a conn_factory callable returning a fresh
  connection per call, a filesystem path (``str`` / ``Path``) to the
  SQLite file, or ``None`` (the app's configured default DB);
* ids are ``uuid4().hex``; timestamps are epoch milliseconds;
* the schema is ensured lazily on every entry point
  (:func:`app.reporting.schema.ensure_reports_schema`), so the store
  works on databases that have not run migrations.

The DB row is the durable artefact: it carries the full
``report_json`` (the source of truth) and the rendered
``markdown_text``, plus listing metadata (channel_id, scope,
project_id, period, generated_at, title).  File-store copies are
optional extras whose paths are recorded on the row when written.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional, Union

from app.reporting.schema import ensure_reports_schema

__all__ = [
    "open_conn",
    "save_report",
    "list_reports",
    "get_report_row",
    "report_metadata",
]

Source = Union[sqlite3.Connection, Any, str, Path, None]


def open_conn(source: Source) -> tuple[sqlite3.Connection, bool]:
    """Return ``(conn, should_close)`` for any accepted source shape.

    NOTE: ``sqlite3.Connection`` objects report ``callable() == True``
    in CPython, so the connection check must come first (the same
    note the Style Lab / Learning stores carry).
    """
    if source is None:
        from app import db as _db

        return _db.get_connection(), True
    if hasattr(source, "execute"):
        conn = source  # type: ignore[assignment]
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        return conn, False  # type: ignore[return-value]
    if isinstance(source, (str, Path)):
        from app import db as _db

        return _db.get_connection(str(source)), True
    if callable(source):
        conn = source()
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        return conn, True
    raise TypeError(
        "source must be a sqlite3 connection, a conn_factory callable, "
        "a DB path, or None"
    )


def _close(conn: sqlite3.Connection, should_close: bool) -> None:
    if should_close:
        try:
            conn.close()
        except Exception:
            pass


def _commit_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def report_metadata(row: Any) -> dict[str, Any]:
    """Listing metadata for one ``generated_reports`` row (no payload)."""
    data = dict(row)
    period: dict[str, Any] = {
        "start_ms": data.get("period_start_ms"),
        "end_ms": data.get("period_end_ms"),
        "label": data.get("period_label"),
    }
    generated_ms = data.get("generated_at")
    generated_iso = None
    if generated_ms is not None:
        from datetime import datetime, timezone

        generated_iso = (
            datetime.fromtimestamp(int(generated_ms) / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return {
        "report_id": data.get("id"),
        "id": data.get("id"),
        "channel_id": data.get("channel_id"),
        "scope": data.get("scope"),
        "project_id": data.get("project_id"),
        "period": period,
        "period_start_ms": data.get("period_start_ms"),
        "period_end_ms": data.get("period_end_ms"),
        "period_label": data.get("period_label"),
        "title": data.get("title"),
        "generated_at_ms": generated_ms,
        "generated_at": generated_iso,
        "json_path": data.get("json_path"),
        "markdown_path": data.get("markdown_path"),
        "has_markdown": data.get("markdown_text") is not None,
    }


def save_report(
    source: Source,
    report: dict[str, Any],
    markdown_text: str,
    *,
    json_path: Optional[str] = None,
    markdown_path: Optional[str] = None,
) -> dict[str, Any]:
    """Insert one generated report; return its listing metadata.

    The caller (the service layer) owns file writing; this function
    only records the paths it is given.
    """
    period = report.get("period") or {}
    conn, should_close = open_conn(source)
    try:
        ensure_reports_schema(conn)
        conn.execute(
            "INSERT INTO generated_reports ("
            " id, channel_id, scope, project_id, period_start_ms,"
            " period_end_ms, period_label, title, generated_at,"
            " json_path, markdown_path, report_json, markdown_text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(report["report_id"]),
                str(report["channel_id"]),
                str(report["scope"]),
                report.get("project_id"),
                period.get("start_ms"),
                period.get("end_ms"),
                period.get("label"),
                report.get("title"),
                int(report["generated_at_ms"]),
                json_path,
                markdown_path,
                json.dumps(report, ensure_ascii=False),
                markdown_text,
            ),
        )
        _commit_quietly(conn)
        row = conn.execute(
            "SELECT * FROM generated_reports WHERE id = ?",
            (str(report["report_id"]),),
        ).fetchone()
        assert row is not None
        return report_metadata(row)
    finally:
        _close(conn, should_close)


def list_reports(
    source: Source,
    *,
    channel_id: Optional[str] = None,
    scope: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Stored reports, newest first, as listing metadata dicts.

    Every filter is optional; ``channel_id`` scoping is the UI's
    primary axis.  Returns ``[]`` on a database with no reports.
    """
    conn, should_close = open_conn(source)
    try:
        ensure_reports_schema(conn)
        clauses: list[str] = []
        params: list[Any] = []
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(str(channel_id))
        if scope is not None:
            clauses.append("scope = ?")
            params.append(str(scope))
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(str(project_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))
        rows = conn.execute(
            f"SELECT * FROM generated_reports{where}"
            " ORDER BY generated_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (*params, safe_limit, safe_offset),
        ).fetchall()
        return [report_metadata(r) for r in rows]
    finally:
        _close(conn, should_close)


def get_report_row(source: Source, report_id: Any) -> Optional[dict[str, Any]]:
    """The full stored row for ``report_id`` (incl. payloads), or None."""
    conn, should_close = open_conn(source)
    try:
        ensure_reports_schema(conn)
        row = conn.execute(
            "SELECT * FROM generated_reports WHERE id = ?",
            (str(report_id),),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        _close(conn, should_close)
