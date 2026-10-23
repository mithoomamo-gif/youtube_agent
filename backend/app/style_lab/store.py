"""SQLite store for Style Lab competitors, videos and style profiles.

A small store class over the tables created by
:mod:`app.style_lab.schema` (``competitor_channels``,
``competitor_videos``, ``style_profiles``), following the codebase
patterns of :mod:`app.queue` / :mod:`app.execution.budget_reservations`:

* constructed with a ``conn_factory`` returning a fresh
  ``sqlite3.Connection`` per call (e.g.
  ``functools.partial(app.db.connect, path)``); a bare connection may
  also be passed, in which case it is used as-is and never closed;
* ids are ``uuid4().hex``; timestamps are epoch milliseconds;
* the schema is ensured lazily on every entry point, so the store
  works on databases that have not run migrations.

Owner scoping (the multi-channel isolation rule)
------------------------------------------------
Every competitor and profile belongs to exactly one *owner* channel
(``channel_id`` FK -> ``channels`` — the *user's* channel the style
profile is being built for).  Scope rules enforced here:

* ``list_*`` methods always filter by the owner ``channel_id``.
* Reads that take an optional ``channel_id`` return ``None`` / ``[]``
  when the row belongs to a different channel — a cross-channel read
  looks exactly like "not found", so existence never leaks.
* Writes that take a ``channel_id`` raise :class:`StyleLabScopeError`
  when the target row belongs to a different channel.
* :meth:`StyleLabStore.create_profile` refuses competitor ids owned
  by another channel (:class:`StyleLabScopeError`) or unknown ids
  (:class:`StyleLabNotFoundError`).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterable, Mapping, Optional

from app.style_lab.parsing import ParsedCompetitor, handle_key


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure the Style Lab tables exist on ``conn``.

    The DDL lives in :mod:`app.style_lab.schema` (sibling-owned); it is
    imported lazily here so this module stays importable while that
    module is being built in parallel.
    """
    from app.style_lab.schema import ensure_style_lab_schema

    ensure_style_lab_schema(conn)

__all__ = [
    "StyleLabStore",
    "StyleLabStoreError",
    "StyleLabNotFoundError",
    "StyleLabScopeError",
    "VIDEO_SOURCES",
    "ANALYSIS_STATUSES",
    "PROFILE_STATUSES",
]

#: competitor_videos.source values (mirror the schema CHECK).
VIDEO_SOURCES = ("attached_file", "url_metadata", "pasted_metadata")
#: competitor_videos.analysis_status values.
ANALYSIS_STATUSES = (
    "not_queued",
    "queued",
    "analysing",
    "analysed",
    "analysis_failed",
)
#: style_profiles.status values.
PROFILE_STATUSES = ("draft", "active", "superseded")


class StyleLabStoreError(Exception):
    """Base class for Style Lab store errors."""


class StyleLabNotFoundError(StyleLabStoreError, LookupError):
    """A referenced competitor / video / profile row does not exist."""


class StyleLabScopeError(StyleLabStoreError, PermissionError):
    """A row exists but belongs to a different owner channel."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    return uuid.uuid4().hex


def _parse_json(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


class StyleLabStore:
    """CRUD + dedupe + owner scoping over the Style Lab tables."""

    def __init__(self, conn_factory: Any) -> None:
        """``conn_factory`` is a callable returning a fresh
        ``sqlite3.Connection``, or a single ``sqlite3.Connection`` to
        reuse (never closed by the store)."""
        # NOTE: sqlite3.Connection objects report callable() == True in
        # CPython, so the connection check must come first.
        if hasattr(conn_factory, "execute"):
            self._conn_factory = None
            self._shared_conn = conn_factory
        elif callable(conn_factory):
            self._conn_factory = conn_factory
            self._shared_conn: Optional[sqlite3.Connection] = None
        else:
            raise TypeError(
                "conn_factory must be callable or a sqlite3 connection"
            )

    # ------------------------------------------------------------------
    # connection / transaction plumbing (same style as app.queue)
    # ------------------------------------------------------------------
    def _connect(self) -> tuple[sqlite3.Connection, bool]:
        """Return ``(conn, should_close)``."""
        if self._shared_conn is not None:
            conn = self._shared_conn
            should_close = False
        else:
            conn = self._conn_factory()  # type: ignore[misc]
            should_close = True
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
        except Exception:
            pass
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        _ensure_schema(conn)
        return conn, should_close

    @contextmanager
    def _write_tx(self, conn: sqlite3.Connection):
        """BEGIN IMMEDIATE transaction; a no-op if one is already open
        (shared-connection callers manage their own transaction)."""
        if conn.in_transaction:
            yield conn
            return
        previous_isolation = conn.isolation_level
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            conn.execute("COMMIT")
        finally:
            try:
                conn.isolation_level = previous_isolation
            except Exception:
                pass

    @staticmethod
    def _close(conn: sqlite3.Connection, should_close: bool) -> None:
        if should_close:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # row shaping
    # ------------------------------------------------------------------
    @staticmethod
    def _competitor_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        data["metadata"] = _parse_json(data.get("metadata_json"))
        return data

    @staticmethod
    def _video_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        tags = _parse_json(data.get("tags_json"))
        data["tags"] = tags if isinstance(tags, list) else []
        return data

    @staticmethod
    def _profile_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        ids = _parse_json(data.get("competitor_ids_json"))
        data["competitor_ids"] = ids if isinstance(ids, list) else []
        data["profile"] = _parse_json(data.get("profile_json"))
        return data

    # ------------------------------------------------------------------
    # scope helpers
    # ------------------------------------------------------------------
    def _fetch_competitor(self, conn, competitor_id: Any) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM competitor_channels WHERE id = ?",
            (str(competitor_id),),
        ).fetchone()
        return self._competitor_dict(row)

    @staticmethod
    def _check_scope(row: Mapping[str, Any], channel_id: Optional[str],
                     what: str) -> None:
        if channel_id is not None and str(row["channel_id"]) != str(channel_id):
            raise StyleLabScopeError(
                f"{what} {row['id']} belongs to channel "
                f"{row['channel_id']}, not {channel_id}"
            )

    # ==================================================================
    # Competitors
    # ==================================================================
    def create_competitor(
        self,
        channel_id: str,
        parsed: ParsedCompetitor | Mapping[str, Any],
        display_name: Optional[str] = None,
    ) -> dict:
        """Insert a competitor for owner ``channel_id``; return its row.

        Service-level dedupe: if an *active* competitor already exists
        for this owner channel with the same ``external_channel_id``
        (when known) or the same normalised (case-insensitive) handle,
        that existing row is returned instead of inserting a duplicate.
        When the duplicate carries identifiers the existing row lacks
        (e.g. it was created from a handle and now a channel id is
        known), the missing fields are backfilled onto the existing row.
        """
        if isinstance(parsed, ParsedCompetitor):
            fields = {
                "source_kind": parsed.source_kind,
                "external_channel_id": parsed.external_channel_id,
                "handle": parsed.handle,
                "channel_url": parsed.channel_url,
                "source_input": parsed.source_input,
            }
        else:
            fields = dict(parsed)
        source_kind = fields.get("source_kind")
        external_channel_id = fields.get("external_channel_id")
        handle = fields.get("handle")
        channel_url = fields.get("channel_url")
        source_input = (
            fields.get("source_input")
            or channel_url
            or (f"@{handle}" if handle else None)
            or external_channel_id
            or ""
        )

        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                existing = self._find_duplicate(
                    conn, channel_id, external_channel_id, handle
                )
                if existing is not None:
                    return self._backfill_duplicate(
                        conn, existing, external_channel_id, channel_url
                    )
                now = _now_ms()
                row_id = _new_id()
                conn.execute(
                    "INSERT INTO competitor_channels ("
                    " id, channel_id, platform, source_input, source_kind,"
                    " external_channel_id, handle, display_name,"
                    " channel_url, metadata_status, metadata_json, status,"
                    " created_at, updated_at)"
                    " VALUES (?, ?, 'youtube', ?, ?, ?, ?, ?, ?,"
                    " 'not_fetched', NULL, 'active', ?, ?)",
                    (
                        row_id,
                        str(channel_id),
                        source_input,
                        source_kind,
                        external_channel_id,
                        handle,
                        display_name,
                        channel_url,
                        now,
                        now,
                    ),
                )
                result = self._fetch_competitor(conn, row_id)
                assert result is not None
                return result
        finally:
            self._close(conn, should_close)

    def _find_duplicate(
        self,
        conn,
        channel_id: str,
        external_channel_id: Optional[str],
        handle: Optional[str],
    ) -> Optional[dict]:
        row = None
        if external_channel_id:
            row = conn.execute(
                "SELECT * FROM competitor_channels WHERE channel_id = ?"
                " AND status = 'active' AND external_channel_id = ?"
                " ORDER BY created_at ASC LIMIT 1",
                (str(channel_id), external_channel_id),
            ).fetchone()
        if row is None and handle_key(handle):
            row = conn.execute(
                "SELECT * FROM competitor_channels WHERE channel_id = ?"
                " AND status = 'active' AND handle IS NOT NULL"
                " AND LOWER(handle) = ?"
                " ORDER BY created_at ASC LIMIT 1",
                (str(channel_id), handle_key(handle)),
            ).fetchone()
        return self._competitor_dict(row)

    def _backfill_duplicate(
        self,
        conn,
        existing: dict,
        external_channel_id: Optional[str],
        channel_url: Optional[str],
    ) -> dict:
        """Fill identifier gaps on a dedupe hit; return the fresh row."""
        sets: list[str] = []
        params: list[Any] = []
        if external_channel_id and not existing.get("external_channel_id"):
            sets.append("external_channel_id = ?")
            params.append(external_channel_id)
        if channel_url and not existing.get("channel_url"):
            sets.append("channel_url = ?")
            params.append(channel_url)
        if not sets:
            return existing
        sets.append("updated_at = ?")
        params.append(_now_ms())
        params.append(existing["id"])
        conn.execute(
            f"UPDATE competitor_channels SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        result = self._fetch_competitor(conn, existing["id"])
        assert result is not None
        return result

    def get_competitor(
        self, competitor_id: Any, *, channel_id: Optional[str] = None
    ) -> Optional[dict]:
        """One competitor by id, or ``None``.

        With ``channel_id`` given, a competitor owned by a different
        channel reads as ``None`` (no existence leak across channels).
        """
        conn, should_close = self._connect()
        try:
            row = self._fetch_competitor(conn, competitor_id)
            if row is None:
                return None
            if channel_id is not None and str(row["channel_id"]) != str(channel_id):
                return None
            return row
        finally:
            self._close(conn, should_close)

    def list_competitors(
        self, channel_id: str, *, include_archived: bool = False
    ) -> list[dict]:
        """All competitors owned by ``channel_id`` (always scoped)."""
        conn, should_close = self._connect()
        try:
            sql = "SELECT * FROM competitor_channels WHERE channel_id = ?"
            if not include_archived:
                sql += " AND status = 'active'"
            sql += " ORDER BY created_at ASC, rowid ASC"
            rows = conn.execute(sql, (str(channel_id),)).fetchall()
            return [d for d in (self._competitor_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)

    def archive_competitor(
        self, competitor_id: Any, *, channel_id: Optional[str] = None
    ) -> Optional[dict]:
        """Set a competitor's status to ``archived``; return the row.

        Raises :class:`StyleLabScopeError` when ``channel_id`` is
        given and the competitor belongs to another channel.  Returns
        ``None`` for an unknown id.
        """
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                row = self._fetch_competitor(conn, competitor_id)
                if row is None:
                    return None
                self._check_scope(row, channel_id, "competitor")
                conn.execute(
                    "UPDATE competitor_channels SET status = 'archived',"
                    " updated_at = ? WHERE id = ?",
                    (_now_ms(), row["id"]),
                )
                return self._fetch_competitor(conn, row["id"])
        finally:
            self._close(conn, should_close)

    # ==================================================================
    # Competitor videos
    # ==================================================================
    #: Columns add_video may write (besides id / competitor_id / status /
    #: timestamps, which the store manages).
    _VIDEO_FIELDS = (
        "source",
        "external_video_id",
        "video_url",
        "title",
        "description",
        "storage_path",
        "file_sha256",
        "file_size_bytes",
        "mime_type",
        "duration_s",
        "width",
        "height",
        "thumbnail_storage_path",
        "thumbnail_sha256",
    )

    def add_video(
        self,
        competitor_id: Any,
        fields: Optional[Mapping[str, Any]] = None,
        *,
        channel_id: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """Attach a video row to a competitor; return the row.

        ``fields`` (a mapping) and keyword arguments are merged
        (keywords win).  ``source`` is required and must be one of
        :data:`VIDEO_SOURCES`; ``tags`` (a list) is serialised into
        ``tags_json`` (an explicit ``tags_json`` string also works).
        New rows start with ``analysis_status='not_queued'``.

        Raises :class:`StyleLabNotFoundError` for an unknown
        competitor, :class:`StyleLabScopeError` when ``channel_id`` is
        given and the competitor belongs to another channel, and
        ``ValueError`` for an invalid ``source``.
        """
        merged: dict[str, Any] = dict(fields or {})
        merged.update(kwargs)
        source = merged.get("source")
        if source not in VIDEO_SOURCES:
            raise ValueError(
                f"video source must be one of {VIDEO_SOURCES}, got {source!r}"
            )
        tags = merged.pop("tags", None)
        if tags is not None:
            merged["tags_json"] = json.dumps(list(tags))
        values: dict[str, Any] = {
            col: merged.get(col) for col in self._VIDEO_FIELDS
        }
        values["tags_json"] = merged.get("tags_json")

        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                competitor = self._fetch_competitor(conn, competitor_id)
                if competitor is None:
                    raise StyleLabNotFoundError(
                        f"competitor {competitor_id} does not exist"
                    )
                self._check_scope(competitor, channel_id, "competitor")
                now = _now_ms()
                row_id = _new_id()
                cols = ["id", "competitor_channel_id", *self._VIDEO_FIELDS,
                        "tags_json", "analysis_status",
                        "analysis_requested_at", "created_at", "updated_at"]
                conn.execute(
                    f"INSERT INTO competitor_videos ({', '.join(cols)})"
                    f" VALUES ({', '.join('?' for _ in cols)})",
                    [
                        row_id,
                        competitor["id"],
                        *[values[c] for c in self._VIDEO_FIELDS],
                        values["tags_json"],
                        "not_queued",
                        None,
                        now,
                        now,
                    ],
                )
                result = self._fetch_video(conn, row_id)
                assert result is not None
                return result
        finally:
            self._close(conn, should_close)

    def _fetch_video(self, conn, video_id: Any) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM competitor_videos WHERE id = ?",
            (str(video_id),),
        ).fetchone()
        return self._video_dict(row)

    def _video_owner_channel_id(self, conn, video: Mapping[str, Any]):
        row = conn.execute(
            "SELECT channel_id FROM competitor_channels WHERE id = ?",
            (video["competitor_channel_id"],),
        ).fetchone()
        return row[0] if row is not None else None

    def get_video(
        self, video_id: Any, *, channel_id: Optional[str] = None
    ) -> Optional[dict]:
        """One video by id, or ``None`` (cross-channel reads as None)."""
        conn, should_close = self._connect()
        try:
            video = self._fetch_video(conn, video_id)
            if video is None:
                return None
            if channel_id is not None:
                owner = self._video_owner_channel_id(conn, video)
                if owner is None or str(owner) != str(channel_id):
                    return None
            return video
        finally:
            self._close(conn, should_close)

    def list_videos(
        self, competitor_id: Any, *, channel_id: Optional[str] = None
    ) -> list[dict]:
        """Videos attached to a competitor, oldest first.

        With ``channel_id`` given, a competitor owned by another
        channel lists as ``[]``.
        """
        conn, should_close = self._connect()
        try:
            competitor = self._fetch_competitor(conn, competitor_id)
            if competitor is None:
                return []
            if channel_id is not None and str(competitor["channel_id"]) != str(channel_id):
                return []
            rows = conn.execute(
                "SELECT * FROM competitor_videos"
                " WHERE competitor_channel_id = ?"
                " ORDER BY created_at ASC, rowid ASC",
                (competitor["id"],),
            ).fetchall()
            return [d for d in (self._video_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)

    def set_analysis_status(
        self,
        video_id: Any,
        status: str,
        *,
        channel_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Set a video's ``analysis_status``; return the updated row.

        Transitioning to ``'queued'`` stamps ``analysis_requested_at``
        with the current time; other transitions leave it untouched.
        Raises ``ValueError`` for a status outside
        :data:`ANALYSIS_STATUSES`, :class:`StyleLabScopeError` on a
        cross-channel write, and returns ``None`` for an unknown id.
        """
        if status not in ANALYSIS_STATUSES:
            raise ValueError(
                f"analysis status must be one of {ANALYSIS_STATUSES},"
                f" got {status!r}"
            )
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                video = self._fetch_video(conn, video_id)
                if video is None:
                    return None
                if channel_id is not None:
                    owner = self._video_owner_channel_id(conn, video)
                    if owner is None or str(owner) != str(channel_id):
                        raise StyleLabScopeError(
                            f"video {video_id} belongs to channel {owner},"
                            f" not {channel_id}"
                        )
                now = _now_ms()
                if status == "queued":
                    conn.execute(
                        "UPDATE competitor_videos SET analysis_status = ?,"
                        " analysis_requested_at = ?, updated_at = ?"
                        " WHERE id = ?",
                        (status, now, now, video["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE competitor_videos SET analysis_status = ?,"
                        " updated_at = ? WHERE id = ?",
                        (status, now, video["id"]),
                    )
                return self._fetch_video(conn, video["id"])
        finally:
            self._close(conn, should_close)

    # ==================================================================
    # Style profiles
    # ==================================================================
    def create_profile(
        self,
        channel_id: str,
        name: str,
        competitor_ids: Iterable[Any] = (),
    ) -> dict:
        """Create a ``draft`` style profile for ``channel_id``.

        Every id in ``competitor_ids`` must exist and belong to
        ``channel_id``: unknown ids raise
        :class:`StyleLabNotFoundError`, another channel's competitors
        raise :class:`StyleLabScopeError` — a profile never spans
        channels.
        """
        ids = [str(c) for c in (competitor_ids or ())]
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                for cid in ids:
                    competitor = self._fetch_competitor(conn, cid)
                    if competitor is None:
                        raise StyleLabNotFoundError(
                            f"competitor {cid} does not exist"
                        )
                    if str(competitor["channel_id"]) != str(channel_id):
                        raise StyleLabScopeError(
                            f"competitor {cid} belongs to channel "
                            f"{competitor['channel_id']}, not {channel_id}"
                        )
                now = _now_ms()
                row_id = _new_id()
                conn.execute(
                    "INSERT INTO style_profiles ("
                    " id, channel_id, name, version, status,"
                    " competitor_ids_json, profile_json, created_at,"
                    " updated_at)"
                    " VALUES (?, ?, ?, 1, 'draft', ?, NULL, ?, ?)",
                    (row_id, str(channel_id), name, json.dumps(ids), now, now),
                )
                result = self._fetch_profile(conn, row_id)
                assert result is not None
                return result
        finally:
            self._close(conn, should_close)

    def _fetch_profile(self, conn, profile_id: Any) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM style_profiles WHERE id = ?",
            (str(profile_id),),
        ).fetchone()
        return self._profile_dict(row)

    def get_profile(
        self, profile_id: Any, *, channel_id: Optional[str] = None
    ) -> Optional[dict]:
        """One profile by id, or ``None`` (cross-channel reads as None)."""
        conn, should_close = self._connect()
        try:
            profile = self._fetch_profile(conn, profile_id)
            if profile is None:
                return None
            if channel_id is not None and str(profile["channel_id"]) != str(channel_id):
                return None
            return profile
        finally:
            self._close(conn, should_close)

    def list_profiles(self, channel_id: str) -> list[dict]:
        """All style profiles owned by ``channel_id`` (always scoped)."""
        conn, should_close = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM style_profiles WHERE channel_id = ?"
                " ORDER BY created_at ASC, rowid ASC",
                (str(channel_id),),
            ).fetchall()
            return [d for d in (self._profile_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)

    def activate_profile(
        self, profile_id: Any, *, channel_id: Optional[str] = None
    ) -> Optional[dict]:
        """Make a profile its channel's ``active`` profile.

        The channel's previously active profile (if any) is moved to
        ``superseded`` in the same transaction, so a channel always has
        at most one active profile.  Raises
        :class:`StyleLabScopeError` on a cross-channel write; returns
        ``None`` for an unknown id.
        """
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                profile = self._fetch_profile(conn, profile_id)
                if profile is None:
                    return None
                self._check_scope(profile, channel_id, "style profile")
                now = _now_ms()
                conn.execute(
                    "UPDATE style_profiles SET status = 'superseded',"
                    " updated_at = ? WHERE channel_id = ?"
                    " AND status = 'active' AND id != ?",
                    (now, profile["channel_id"], profile["id"]),
                )
                conn.execute(
                    "UPDATE style_profiles SET status = 'active',"
                    " updated_at = ? WHERE id = ?",
                    (now, profile["id"]),
                )
                return self._fetch_profile(conn, profile["id"])
        finally:
            self._close(conn, should_close)
