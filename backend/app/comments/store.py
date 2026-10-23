"""SQLite store for Comments Intelligence (Round 5 — Agent 28).

A small store class over the tables created by
:mod:`app.comments.schema` (``comments``, ``comment_sync_state``,
``comment_analyses``, ``comment_drafts``,
``comment_moderation_flags``), following the codebase patterns of
:mod:`app.style_lab.store`:

* constructed with a ``conn_factory`` returning a fresh
  ``sqlite3.Connection`` per call (e.g.
  ``functools.partial(app.db.connect, path)``); a bare connection may
  also be passed, in which case it is used as-is and never closed;
* comment rows are keyed by the YouTube comment id; analysis / draft /
  flag ids are ``uuid4().hex``; timestamps are epoch milliseconds;
* the schema is ensured lazily on every entry point, so the store
  works on databases that have not run migrations.

Owner scoping (the multi-channel isolation rule)
------------------------------------------------
Every comment belongs to exactly one *owner* channel (``channel_id``
FK -> ``channels`` — the *user's* channel whose video the comment is
on).  Scope rules enforced here, mirroring the Style Lab precedent:

* ``list_*`` methods always filter by the owner ``channel_id``.
* Reads that take an optional ``channel_id`` return ``None`` / ``[]``
  when the row belongs to a different channel — a cross-channel read
  looks exactly like "not found", so existence never leaks.
* Writes that take a ``channel_id`` raise :class:`CommentsScopeError`
  when the target row belongs to a different channel.
* Drafts / flags are created against a comment; their ``channel_id``
  is copied FROM the comment row (never trusted from the caller) and
  a caller-supplied ``channel_id`` that disagrees raises
  :class:`CommentsScopeError`.

State machines
--------------
* Drafts: ``proposed`` -> ``approved`` | ``rejected`` |
  ``discarded`` only.  ``discarded`` is a DISTINCT terminal state
  from ``rejected`` (a draft the reviewer simply throws away,
  rather than deciding against).  Terminal states are immutable —
  any further transition (including back to ``proposed``) raises
  :class:`CommentsStateError`.  A draft's text may be edited ONLY
  while ``proposed`` (:meth:`CommentsStore.update_draft_text`),
  with the edit recorded in the draft's provenance.
* Moderation flags: ``pending`` -> ``dismissed`` | ``actioned`` only,
  same terminal-immutability rule.  A flag's status only RECORDS a
  human decision; nothing here ever executes a moderation action.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional


def _ensure_schema(conn: sqlite3.Connection) -> None:
    from app.comments.schema import ensure_comments_schema

    ensure_comments_schema(conn)


__all__ = [
    "CommentsStore",
    "CommentsStoreError",
    "CommentsNotFoundError",
    "CommentsScopeError",
    "CommentsStateError",
    "SYNC_STATUSES",
    "ANALYSIS_SCOPES",
    "ANALYSIS_ENGINES",
    "DRAFT_STATUSES",
    "DRAFT_TRANSITIONS",
    "FLAG_KINDS",
    "FLAG_STATUSES",
    "FLAG_TRANSITIONS",
]

#: comment_sync_state.status values (mirror the schema CHECK).
SYNC_STATUSES = ("never_synced", "synced", "not_connected", "error")
#: comment_analyses.scope values.
ANALYSIS_SCOPES = ("video", "channel")
#: comment_analyses.engine values.
ANALYSIS_ENGINES = ("llm", "deterministic")
#: comment_drafts.status values.
DRAFT_STATUSES = ("proposed", "approved", "rejected", "discarded")
#: Legal draft transitions: proposed -> approved|rejected|discarded only.
DRAFT_TRANSITIONS = {"proposed": ("approved", "rejected", "discarded")}
#: comment_moderation_flags.flag_kind values.
FLAG_KINDS = ("spam", "abuse", "other")
#: comment_moderation_flags.status values.
FLAG_STATUSES = ("pending", "dismissed", "actioned")
#: Legal flag transitions: pending -> dismissed|actioned only.
FLAG_TRANSITIONS = {"pending": ("dismissed", "actioned")}


class CommentsStoreError(Exception):
    """Base class for Comments store errors."""


class CommentsNotFoundError(CommentsStoreError, LookupError):
    """A referenced comment / draft / flag row does not exist."""


class CommentsScopeError(CommentsStoreError, PermissionError):
    """A row exists but belongs to a different owner channel."""


class CommentsStateError(CommentsStoreError, ValueError):
    """An illegal status transition (e.g. out of a terminal state)."""


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


def _dump_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _coerce_epoch_ms(value: Any) -> Optional[int]:
    """Accept epoch ms (int/float), epoch s, or an RFC3339/ISO string."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid timestamp {value!r}")
    if isinstance(value, (int, float)):
        v = int(value)
        # Heuristic: values below ~2001-09-09 in ms are epoch seconds.
        return v * 1000 if 0 < v < 1_000_000_000_000 else v
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _coerce_epoch_ms(int(text))
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    raise ValueError(f"invalid timestamp {value!r}")


class CommentsStore:
    """Owner-scoped CRUD + idempotent upsert over the Comments tables."""

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
    # connection / transaction plumbing (same style as app.style_lab.store)
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
    def _comment_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        data["is_top_level"] = bool(data.get("is_top_level"))
        data["raw"] = _parse_json(data.get("raw_json"))
        return data

    @staticmethod
    def _sync_state_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        return dict(row)

    @staticmethod
    def _analysis_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        data["rollup"] = _parse_json(data.get("rollup_json"))
        data["provenance"] = _parse_json(data.get("provenance_json"))
        return data

    @staticmethod
    def _draft_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        data["provenance"] = _parse_json(data.get("provenance_json"))
        return data

    @staticmethod
    def _flag_dict(row: Any) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        data["provenance"] = _parse_json(data.get("provenance_json"))
        return data

    # ------------------------------------------------------------------
    # scope helpers
    # ------------------------------------------------------------------
    def _fetch_comment(self, conn, comment_id: Any) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM comments WHERE id = ?", (str(comment_id),)
        ).fetchone()
        return self._comment_dict(row)

    @staticmethod
    def _check_scope(row: Mapping[str, Any], channel_id: Optional[str],
                     what: str) -> None:
        if channel_id is not None and str(row["channel_id"]) != str(channel_id):
            raise CommentsScopeError(
                f"{what} {row['id']} belongs to channel "
                f"{row['channel_id']}, not {channel_id}"
            )

    def _resolve_comment(self, conn, comment_id: Any,
                         channel_id: Optional[str]) -> dict:
        comment = self._fetch_comment(conn, comment_id)
        if comment is None:
            raise CommentsNotFoundError(
                f"comment {comment_id} does not exist"
            )
        self._check_scope(comment, channel_id, "comment")
        return comment

    # ==================================================================
    # Comments — idempotent upsert + scoped reads
    # ==================================================================
    def upsert_comment(
        self,
        channel_id: str,
        comment: Mapping[str, Any],
        *,
        fetched_at: Optional[int] = None,
    ) -> dict:
        """Insert or refresh one comment; return the stored row.

        ``comment`` keys (aliases in parentheses are also accepted):
        ``id`` (``comment_id`` / ``youtube_comment_id``) — required,
        the YouTube comment id; ``video_id`` — required, the YouTube
        video id; ``text`` (``text_display`` / ``text_original``);
        ``author_display_name``; ``author_channel_id``;
        ``like_count``; ``reply_count``; ``parent_comment_id``;
        ``is_top_level`` (derived from ``parent_comment_id`` when not
        given); ``published_at`` (epoch ms, epoch s, or RFC3339
        string); ``project_id``; ``raw`` / ``raw_json`` (optional
        debug stash).

        Idempotent: re-upserting the same comment id refreshes the
        mutable fields (text, author, like/reply counts, threading,
        published_at, fetched_at, raw) and ``updated_at``; it never
        creates a second row and never moves the comment to another
        channel — an existing id owned by a different channel raises
        :class:`CommentsScopeError`.
        """
        fields = dict(comment or {})
        comment_id = (
            fields.get("id") or fields.get("comment_id")
            or fields.get("youtube_comment_id")
        )
        if not comment_id:
            raise ValueError("comment mapping requires an 'id'")
        video_id = fields.get("video_id")
        if not video_id:
            raise ValueError("comment mapping requires a 'video_id'")
        text = (
            fields.get("text") or fields.get("text_display")
            or fields.get("text_original") or ""
        )
        parent_comment_id = fields.get("parent_comment_id")
        if "is_top_level" in fields and fields["is_top_level"] is not None:
            is_top_level = 1 if fields["is_top_level"] else 0
        else:
            is_top_level = 0 if parent_comment_id else 1
        now = _now_ms()
        fetched = int(fetched_at) if fetched_at is not None else now
        values = {
            "channel_id": str(channel_id),
            "video_id": str(video_id),
            "project_id": fields.get("project_id"),
            "author_display_name": fields.get("author_display_name"),
            "author_channel_id": fields.get("author_channel_id"),
            "text": str(text),
            "like_count": int(fields.get("like_count") or 0),
            "reply_count": int(fields.get("reply_count") or 0),
            "parent_comment_id": parent_comment_id,
            "is_top_level": is_top_level,
            "published_at": _coerce_epoch_ms(fields.get("published_at")),
            "fetched_at": fetched,
            "raw_json": _dump_json(
                fields.get("raw") if "raw" in fields
                else fields.get("raw_json")
            ),
        }

        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                existing = self._fetch_comment(conn, comment_id)
                if existing is not None:
                    self._check_scope(existing, channel_id, "comment")
                    conn.execute(
                        "UPDATE comments SET video_id = ?,"
                        " project_id = COALESCE(?, project_id),"
                        " author_display_name = ?, author_channel_id = ?,"
                        " text = ?, like_count = ?, reply_count = ?,"
                        " parent_comment_id = ?, is_top_level = ?,"
                        " published_at = COALESCE(?, published_at),"
                        " fetched_at = ?,"
                        " raw_json = COALESCE(?, raw_json),"
                        " updated_at = ? WHERE id = ?",
                        (
                            values["video_id"], values["project_id"],
                            values["author_display_name"],
                            values["author_channel_id"], values["text"],
                            values["like_count"], values["reply_count"],
                            values["parent_comment_id"],
                            values["is_top_level"], values["published_at"],
                            values["fetched_at"], values["raw_json"],
                            now, str(comment_id),
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO comments ("
                        " id, channel_id, video_id, project_id,"
                        " author_display_name, author_channel_id, text,"
                        " like_count, reply_count, parent_comment_id,"
                        " is_top_level, published_at, fetched_at, raw_json,"
                        " created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                        " ?, ?, ?)",
                        (
                            str(comment_id), values["channel_id"],
                            values["video_id"], values["project_id"],
                            values["author_display_name"],
                            values["author_channel_id"], values["text"],
                            values["like_count"], values["reply_count"],
                            values["parent_comment_id"],
                            values["is_top_level"], values["published_at"],
                            values["fetched_at"], values["raw_json"],
                            now, now,
                        ),
                    )
                result = self._fetch_comment(conn, comment_id)
                assert result is not None
                return result
        finally:
            self._close(conn, should_close)

    def upsert_comments(
        self,
        channel_id: str,
        comments: Iterable[Mapping[str, Any]],
        *,
        video_id: Optional[str] = None,
        fetched_at: Optional[int] = None,
    ) -> list[dict]:
        """Bulk idempotent upsert; returns the stored rows in order.

        A per-item ``video_id`` wins; ``video_id=`` here is the
        default for items that omit it (the usual sync shape: one
        video's comment page at a time).
        """
        rows: list[dict] = []
        for item in comments or ():
            fields = dict(item)
            if not fields.get("video_id") and video_id:
                fields["video_id"] = video_id
            rows.append(
                self.upsert_comment(channel_id, fields, fetched_at=fetched_at)
            )
        return rows

    def get_comment(
        self, comment_id: Any, *, channel_id: Optional[str] = None
    ) -> Optional[dict]:
        """One comment by YouTube comment id, or ``None``.

        With ``channel_id`` given, a comment owned by a different
        channel reads as ``None`` (no existence leak across channels).
        """
        conn, should_close = self._connect()
        try:
            row = self._fetch_comment(conn, comment_id)
            if row is None:
                return None
            if channel_id is not None and str(row["channel_id"]) != str(channel_id):
                return None
            return row
        finally:
            self._close(conn, should_close)

    def list_comments(
        self,
        channel_id: str,
        video_id: Optional[str] = None,
        *,
        top_level_only: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict]:
        """Comments owned by ``channel_id`` (always scoped), newest
        first by ``published_at`` (falling back to insertion order)."""
        conn, should_close = self._connect()
        try:
            sql = "SELECT * FROM comments WHERE channel_id = ?"
            params: list[Any] = [str(channel_id)]
            if video_id is not None:
                sql += " AND video_id = ?"
                params.append(str(video_id))
            if top_level_only:
                sql += " AND is_top_level = 1"
            sql += (
                " ORDER BY COALESCE(published_at, created_at) DESC,"
                " created_at DESC, rowid DESC"
            )
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params.extend([int(limit), int(offset)])
            rows = conn.execute(sql, params).fetchall()
            return [d for d in (self._comment_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)

    def count_comments(
        self, channel_id: str, video_id: Optional[str] = None
    ) -> int:
        """Number of stored comments for the owner channel (/ video)."""
        conn, should_close = self._connect()
        try:
            sql = "SELECT COUNT(*) FROM comments WHERE channel_id = ?"
            params: list[Any] = [str(channel_id)]
            if video_id is not None:
                sql += " AND video_id = ?"
                params.append(str(video_id))
            return int(conn.execute(sql, params).fetchone()[0])
        finally:
            self._close(conn, should_close)

    def list_replies(
        self, channel_id: str, parent_comment_id: Any
    ) -> list[dict]:
        """Replies to one parent comment, scoped to ``channel_id``."""
        conn, should_close = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM comments WHERE channel_id = ?"
                " AND parent_comment_id = ?"
                " ORDER BY COALESCE(published_at, created_at) ASC,"
                " rowid ASC",
                (str(channel_id), str(parent_comment_id)),
            ).fetchall()
            return [d for d in (self._comment_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)

    # ==================================================================
    # Sync state — the honest-states backbone
    # ==================================================================
    def get_sync_state(self, channel_id: str, video_id: str) -> dict:
        """Sync state for one (channel, video).

        A video that was never synced has NO row; this returns the
        honest synthetic state for that case:
        ``status='never_synced'``, ``comment_count=0``,
        ``last_synced_at=None``, ``error_reason=None`` and timestamps
        ``None`` (plus ``exists=False``; stored rows carry
        ``exists=True``).
        """
        conn, should_close = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM comment_sync_state"
                " WHERE channel_id = ? AND video_id = ?",
                (str(channel_id), str(video_id)),
            ).fetchone()
            if row is None:
                return {
                    "channel_id": str(channel_id),
                    "video_id": str(video_id),
                    "project_id": None,
                    "status": "never_synced",
                    "last_synced_at": None,
                    "comment_count": 0,
                    "error_reason": None,
                    "created_at": None,
                    "updated_at": None,
                    "exists": False,
                }
            data = self._sync_state_dict(row)
            assert data is not None
            data["exists"] = True
            return data
        finally:
            self._close(conn, should_close)

    def set_sync_state(
        self,
        channel_id: str,
        video_id: str,
        status: str,
        *,
        comment_count: Optional[int] = None,
        error_reason: Optional[str] = None,
        project_id: Optional[str] = None,
        last_synced_at: Optional[int] = None,
    ) -> dict:
        """Upsert the sync state for one (channel, video); return it.

        ``status`` must be one of :data:`SYNC_STATUSES` (``ValueError``
        otherwise).  Transitioning to 'synced' stamps
        ``last_synced_at`` (unless explicitly given) and clears
        ``error_reason`` (unless explicitly given).  ``comment_count``
        left as ``None`` keeps the previous count.
        """
        if status not in SYNC_STATUSES:
            raise ValueError(
                f"sync status must be one of {SYNC_STATUSES}, got {status!r}"
            )
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                now = _now_ms()
                existing = conn.execute(
                    "SELECT * FROM comment_sync_state"
                    " WHERE channel_id = ? AND video_id = ?",
                    (str(channel_id), str(video_id)),
                ).fetchone()
                prev = dict(existing) if existing is not None else None
                synced_at = last_synced_at
                if synced_at is None and status == "synced":
                    synced_at = now
                if synced_at is None and prev is not None:
                    synced_at = prev["last_synced_at"]
                count = (
                    comment_count if comment_count is not None
                    else (prev["comment_count"] if prev else 0)
                )
                reason = error_reason
                if reason is None and status == "synced":
                    reason = None
                elif reason is None and prev is not None:
                    reason = prev["error_reason"]
                proj = project_id or (prev["project_id"] if prev else None)
                conn.execute(
                    "INSERT INTO comment_sync_state ("
                    " channel_id, video_id, project_id, status,"
                    " last_synced_at, comment_count, error_reason,"
                    " created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (channel_id, video_id) DO UPDATE SET"
                    " project_id = excluded.project_id,"
                    " status = excluded.status,"
                    " last_synced_at = excluded.last_synced_at,"
                    " comment_count = excluded.comment_count,"
                    " error_reason = excluded.error_reason,"
                    " updated_at = excluded.updated_at",
                    (
                        str(channel_id), str(video_id), proj, status,
                        synced_at, int(count), reason,
                        prev["created_at"] if prev else now, now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM comment_sync_state"
                    " WHERE channel_id = ? AND video_id = ?",
                    (str(channel_id), str(video_id)),
                ).fetchone()
                data = self._sync_state_dict(row)
                assert data is not None
                data["exists"] = True
                return data
        finally:
            self._close(conn, should_close)

    def list_sync_states(
        self, channel_id: str, *, status: Optional[str] = None
    ) -> list[dict]:
        """Stored sync-state rows owned by ``channel_id`` (always
        scoped).  Videos with no row are 'never_synced' — see
        :meth:`get_sync_state`."""
        if status is not None and status not in SYNC_STATUSES:
            raise ValueError(
                f"sync status must be one of {SYNC_STATUSES}, got {status!r}"
            )
        conn, should_close = self._connect()
        try:
            sql = (
                "SELECT * FROM comment_sync_state WHERE channel_id = ?"
            )
            params: list[Any] = [str(channel_id)]
            if status is not None:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY updated_at DESC, video_id ASC"
            rows = conn.execute(sql, params).fetchall()
            out = []
            for r in rows:
                data = self._sync_state_dict(r)
                if data is not None:
                    data["exists"] = True
                    out.append(data)
            return out
        finally:
            self._close(conn, should_close)

    # ==================================================================
    # Analyses — rollups with provenance
    # ==================================================================
    def create_analysis(
        self,
        channel_id: str,
        *,
        engine: str,
        scope: str = "video",
        video_id: Optional[str] = None,
        rollup: Any = None,
        model_id: Optional[str] = None,
        comment_count: int = 0,
        provenance: Any = None,
    ) -> dict:
        """Record one analysis rollup; return the row.

        ``engine`` must be 'llm' or 'deterministic'; ``scope`` 'video'
        requires ``video_id`` (both ``ValueError`` otherwise).
        ``rollup`` / ``provenance`` may be mappings (serialised to
        JSON) or pre-serialised JSON strings.
        """
        if engine not in ANALYSIS_ENGINES:
            raise ValueError(
                f"analysis engine must be one of {ANALYSIS_ENGINES},"
                f" got {engine!r}"
            )
        if scope not in ANALYSIS_SCOPES:
            raise ValueError(
                f"analysis scope must be one of {ANALYSIS_SCOPES},"
                f" got {scope!r}"
            )
        if scope == "video" and not video_id:
            raise ValueError("a video-scope analysis requires video_id")
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                now = _now_ms()
                row_id = _new_id()
                conn.execute(
                    "INSERT INTO comment_analyses ("
                    " id, channel_id, scope, video_id, rollup_json,"
                    " engine, model_id, comment_count, provenance_json,"
                    " created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id, str(channel_id), scope,
                        str(video_id) if video_id else None,
                        _dump_json(rollup), engine, model_id,
                        int(comment_count or 0), _dump_json(provenance),
                        now, now,
                    ),
                )
                result = self._fetch_analysis(conn, row_id)
                assert result is not None
                return result
        finally:
            self._close(conn, should_close)

    def _fetch_analysis(self, conn, analysis_id: Any) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM comment_analyses WHERE id = ?",
            (str(analysis_id),),
        ).fetchone()
        return self._analysis_dict(row)

    def get_analysis(
        self, analysis_id: Any, *, channel_id: Optional[str] = None
    ) -> Optional[dict]:
        """One analysis by id, or ``None`` (cross-channel reads None)."""
        conn, should_close = self._connect()
        try:
            row = self._fetch_analysis(conn, analysis_id)
            if row is None:
                return None
            if channel_id is not None and str(row["channel_id"]) != str(channel_id):
                return None
            return row
        finally:
            self._close(conn, should_close)

    def list_analyses(
        self,
        channel_id: str,
        *,
        video_id: Optional[str] = None,
        scope: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Analyses owned by ``channel_id`` (always scoped), newest first."""
        if scope is not None and scope not in ANALYSIS_SCOPES:
            raise ValueError(
                f"analysis scope must be one of {ANALYSIS_SCOPES},"
                f" got {scope!r}"
            )
        conn, should_close = self._connect()
        try:
            sql = "SELECT * FROM comment_analyses WHERE channel_id = ?"
            params: list[Any] = [str(channel_id)]
            if video_id is not None:
                sql += " AND video_id = ?"
                params.append(str(video_id))
            if scope is not None:
                sql += " AND scope = ?"
                params.append(scope)
            sql += " ORDER BY created_at DESC, rowid DESC"
            if limit is not None:
                sql += " LIMIT ?"
                params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
            return [d for d in (self._analysis_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)

    def latest_analysis(
        self,
        channel_id: str,
        *,
        scope: str = "video",
        video_id: Optional[str] = None,
    ) -> Optional[dict]:
        """The most recent analysis for the scope, or ``None``."""
        rows = self.list_analyses(
            channel_id, video_id=video_id, scope=scope, limit=1
        )
        return rows[0] if rows else None

    # ==================================================================
    # Drafts — guarded state machine
    # (proposed -> approved|rejected|discarded; text editable only
    # while proposed, with provenance)
    # ==================================================================
    def create_draft(
        self,
        comment_id: Any,
        draft_text: str,
        *,
        channel_id: Optional[str] = None,
        created_by: str = "agent28",
        provenance: Any = None,
    ) -> dict:
        """Create a ``proposed`` reply draft for a comment; return it.

        The draft's ``channel_id`` / ``video_id`` are copied from the
        parent comment — never trusted from the caller.  A
        ``channel_id`` that disagrees with the comment's owner raises
        :class:`CommentsScopeError`; an unknown comment raises
        :class:`CommentsNotFoundError`.
        """
        if created_by not in ("agent28", "user"):
            raise ValueError(
                f"created_by must be 'agent28' or 'user', got {created_by!r}"
            )
        if not draft_text or not str(draft_text).strip():
            raise ValueError("draft_text must be non-empty")
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                comment = self._resolve_comment(conn, comment_id, channel_id)
                now = _now_ms()
                row_id = _new_id()
                conn.execute(
                    "INSERT INTO comment_drafts ("
                    " id, comment_id, channel_id, video_id, draft_text,"
                    " status, created_by, provenance_json, decided_at,"
                    " decided_by, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?, NULL, NULL,"
                    " ?, ?)",
                    (
                        row_id, comment["id"], comment["channel_id"],
                        comment["video_id"], str(draft_text), created_by,
                        _dump_json(provenance), now, now,
                    ),
                )
                result = self._fetch_draft(conn, row_id)
                assert result is not None
                return result
        finally:
            self._close(conn, should_close)

    def _fetch_draft(self, conn, draft_id: Any) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM comment_drafts WHERE id = ?", (str(draft_id),)
        ).fetchone()
        return self._draft_dict(row)

    def get_draft(
        self, draft_id: Any, *, channel_id: Optional[str] = None
    ) -> Optional[dict]:
        """One draft by id, or ``None`` (cross-channel reads as None)."""
        conn, should_close = self._connect()
        try:
            row = self._fetch_draft(conn, draft_id)
            if row is None:
                return None
            if channel_id is not None and str(row["channel_id"]) != str(channel_id):
                return None
            return row
        finally:
            self._close(conn, should_close)

    def list_drafts(
        self,
        channel_id: str,
        *,
        status: Optional[str] = None,
        comment_id: Optional[Any] = None,
        video_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Drafts owned by ``channel_id`` (always scoped), newest first."""
        if status is not None and status not in DRAFT_STATUSES:
            raise ValueError(
                f"draft status must be one of {DRAFT_STATUSES}, got {status!r}"
            )
        conn, should_close = self._connect()
        try:
            sql = "SELECT * FROM comment_drafts WHERE channel_id = ?"
            params: list[Any] = [str(channel_id)]
            if status is not None:
                sql += " AND status = ?"
                params.append(status)
            if comment_id is not None:
                sql += " AND comment_id = ?"
                params.append(str(comment_id))
            if video_id is not None:
                sql += " AND video_id = ?"
                params.append(str(video_id))
            sql += " ORDER BY created_at DESC, rowid DESC"
            if limit is not None:
                sql += " LIMIT ?"
                params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
            return [d for d in (self._draft_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)

    def update_draft_text(
        self,
        draft_id: Any,
        draft_text: str,
        *,
        channel_id: Optional[str] = None,
        edited_by: Optional[str] = None,
    ) -> Optional[dict]:
        """Edit a draft's text; return the updated row.

        Legal ONLY while the draft is ``proposed`` — editing a
        terminal draft (``approved`` / ``rejected`` / ``discarded``)
        raises :class:`CommentsStateError`, as does empty text
        (``ValueError``).  Returns ``None`` for an unknown draft id;
        raises :class:`CommentsScopeError` on a cross-channel write.

        Provenance: the edit is recorded on the draft's existing
        ``provenance_json`` — an ``edits`` list is appended with
        ``{edited_by, edited_at, previous_text, new_text}`` per
        edit, and ``last_edited_by`` / ``last_edited_at`` mirror the
        most recent edit.  All pre-existing provenance fields
        (engine / model / prompt_version / …) are preserved.  This
        is a purely LOCAL text update: nothing is posted anywhere.
        """
        if not draft_text or not str(draft_text).strip():
            raise ValueError("draft_text must be non-empty")
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                draft = self._fetch_draft(conn, draft_id)
                if draft is None:
                    return None
                self._check_scope(draft, channel_id, "comment draft")
                if draft["status"] != "proposed":
                    raise CommentsStateError(
                        f"draft {draft['id']} is {draft['status']!r};"
                        " only a 'proposed' draft can be edited"
                    )
                now = _now_ms()
                provenance = draft.get("provenance")
                if not isinstance(provenance, dict):
                    provenance = {}
                edits = provenance.get("edits")
                if not isinstance(edits, list):
                    edits = []
                edits.append({
                    "edited_by": edited_by,
                    "edited_at": now,
                    "previous_text": draft["draft_text"],
                    "new_text": str(draft_text),
                })
                provenance["edits"] = edits
                provenance["last_edited_by"] = edited_by
                provenance["last_edited_at"] = now
                conn.execute(
                    "UPDATE comment_drafts SET draft_text = ?,"
                    " provenance_json = ?, updated_at = ? WHERE id = ?",
                    (
                        str(draft_text), _dump_json(provenance), now,
                        draft["id"],
                    ),
                )
                return self._fetch_draft(conn, draft["id"])
        finally:
            self._close(conn, should_close)

    def set_draft_status(
        self,
        draft_id: Any,
        status: str,
        *,
        channel_id: Optional[str] = None,
        decided_by: Optional[str] = None,
    ) -> Optional[dict]:
        """Transition a draft; return the updated row.

        Only ``proposed`` -> ``approved`` | ``rejected`` |
        ``discarded`` is legal (``discarded`` is a terminal state
        DISTINCT from ``rejected``).  Terminal states are immutable:
        any transition out of ``approved`` / ``rejected`` /
        ``discarded`` (including back to ``proposed``) raises
        :class:`CommentsStateError`, as does a no-op or unknown
        target (unknown targets raise ``ValueError`` via the status
        check).  Returns ``None`` for an unknown draft id; raises
        :class:`CommentsScopeError` on a cross-channel write.  A
        successful decision stamps ``decided_at``.
        """
        if status not in DRAFT_STATUSES:
            raise ValueError(
                f"draft status must be one of {DRAFT_STATUSES}, got {status!r}"
            )
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                draft = self._fetch_draft(conn, draft_id)
                if draft is None:
                    return None
                self._check_scope(draft, channel_id, "comment draft")
                allowed = DRAFT_TRANSITIONS.get(draft["status"], ())
                if status not in allowed:
                    raise CommentsStateError(
                        f"draft {draft['id']} is {draft['status']!r};"
                        f" cannot transition to {status!r}"
                        f" (allowed: {allowed or 'none — terminal'})"
                    )
                now = _now_ms()
                conn.execute(
                    "UPDATE comment_drafts SET status = ?, decided_at = ?,"
                    " decided_by = ?, updated_at = ? WHERE id = ?",
                    (status, now, decided_by, now, draft["id"]),
                )
                return self._fetch_draft(conn, draft["id"])
        finally:
            self._close(conn, should_close)

    #: Readable alias for the engagement worker.
    decide_draft = set_draft_status

    # ==================================================================
    # Moderation flags — record human decisions, NEVER auto-execute
    # ==================================================================
    def create_moderation_flag(
        self,
        comment_id: Any,
        flag_kind: str,
        *,
        channel_id: Optional[str] = None,
        reason: Optional[str] = None,
        suggested_action: Optional[str] = None,
        provenance: Any = None,
    ) -> dict:
        """Flag a comment (status ``pending``); return the flag row.

        ``flag_kind`` must be one of :data:`FLAG_KINDS`.
        ``suggested_action`` is advisory free-form text only — it is
        never executed by this store.  The flag's ``channel_id`` /
        ``video_id`` are copied from the parent comment; a disagreeing
        caller ``channel_id`` raises :class:`CommentsScopeError` and
        an unknown comment raises :class:`CommentsNotFoundError`.
        """
        if flag_kind not in FLAG_KINDS:
            raise ValueError(
                f"flag_kind must be one of {FLAG_KINDS}, got {flag_kind!r}"
            )
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                comment = self._resolve_comment(conn, comment_id, channel_id)
                now = _now_ms()
                row_id = _new_id()
                conn.execute(
                    "INSERT INTO comment_moderation_flags ("
                    " id, comment_id, channel_id, video_id, flag_kind,"
                    " reason, suggested_action, status, provenance_json,"
                    " decided_at, decided_by, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL,"
                    " NULL, ?, ?)",
                    (
                        row_id, comment["id"], comment["channel_id"],
                        comment["video_id"], flag_kind, reason,
                        suggested_action, _dump_json(provenance), now, now,
                    ),
                )
                result = self._fetch_flag(conn, row_id)
                assert result is not None
                return result
        finally:
            self._close(conn, should_close)

    #: Short alias.
    create_flag = create_moderation_flag

    def _fetch_flag(self, conn, flag_id: Any) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM comment_moderation_flags WHERE id = ?",
            (str(flag_id),),
        ).fetchone()
        return self._flag_dict(row)

    def get_moderation_flag(
        self, flag_id: Any, *, channel_id: Optional[str] = None
    ) -> Optional[dict]:
        """One flag by id, or ``None`` (cross-channel reads as None)."""
        conn, should_close = self._connect()
        try:
            row = self._fetch_flag(conn, flag_id)
            if row is None:
                return None
            if channel_id is not None and str(row["channel_id"]) != str(channel_id):
                return None
            return row
        finally:
            self._close(conn, should_close)

    #: Short alias.
    get_flag = get_moderation_flag

    def list_moderation_flags(
        self,
        channel_id: str,
        *,
        status: Optional[str] = None,
        comment_id: Optional[Any] = None,
        video_id: Optional[str] = None,
        flag_kind: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Flags owned by ``channel_id`` (always scoped), newest first."""
        if status is not None and status not in FLAG_STATUSES:
            raise ValueError(
                f"flag status must be one of {FLAG_STATUSES}, got {status!r}"
            )
        if flag_kind is not None and flag_kind not in FLAG_KINDS:
            raise ValueError(
                f"flag_kind must be one of {FLAG_KINDS}, got {flag_kind!r}"
            )
        conn, should_close = self._connect()
        try:
            sql = "SELECT * FROM comment_moderation_flags WHERE channel_id = ?"
            params: list[Any] = [str(channel_id)]
            if status is not None:
                sql += " AND status = ?"
                params.append(status)
            if comment_id is not None:
                sql += " AND comment_id = ?"
                params.append(str(comment_id))
            if video_id is not None:
                sql += " AND video_id = ?"
                params.append(str(video_id))
            if flag_kind is not None:
                sql += " AND flag_kind = ?"
                params.append(flag_kind)
            sql += " ORDER BY created_at DESC, rowid DESC"
            if limit is not None:
                sql += " LIMIT ?"
                params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
            return [d for d in (self._flag_dict(r) for r in rows) if d]
        finally:
            self._close(conn, should_close)

    #: Short alias.
    list_flags = list_moderation_flags

    def set_moderation_flag_status(
        self,
        flag_id: Any,
        status: str,
        *,
        channel_id: Optional[str] = None,
        decided_by: Optional[str] = None,
    ) -> Optional[dict]:
        """Record a human decision on a flag; return the updated row.

        Only ``pending`` -> ``dismissed`` | ``actioned`` is legal;
        terminal states are immutable (:class:`CommentsStateError`).
        This records the decision ONLY — no moderation action is ever
        executed here.  Returns ``None`` for an unknown flag id;
        raises :class:`CommentsScopeError` on a cross-channel write.
        """
        if status not in FLAG_STATUSES:
            raise ValueError(
                f"flag status must be one of {FLAG_STATUSES}, got {status!r}"
            )
        conn, should_close = self._connect()
        try:
            with self._write_tx(conn):
                flag = self._fetch_flag(conn, flag_id)
                if flag is None:
                    return None
                self._check_scope(flag, channel_id, "moderation flag")
                allowed = FLAG_TRANSITIONS.get(flag["status"], ())
                if status not in allowed:
                    raise CommentsStateError(
                        f"moderation flag {flag['id']} is"
                        f" {flag['status']!r}; cannot transition to"
                        f" {status!r} (allowed: {allowed or 'none — terminal'})"
                    )
                now = _now_ms()
                conn.execute(
                    "UPDATE comment_moderation_flags SET status = ?,"
                    " decided_at = ?, decided_by = ?, updated_at = ?"
                    " WHERE id = ?",
                    (status, now, decided_by, now, flag["id"]),
                )
                return self._fetch_flag(conn, flag["id"])
        finally:
            self._close(conn, should_close)

    #: Short alias.
    set_flag_status = set_moderation_flag_status
