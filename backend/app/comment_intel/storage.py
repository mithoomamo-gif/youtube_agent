"""Comment Intelligence — persistence seam.

Two layers, both honest about which one was used:

1. JSON file seam (always available): :class:`CommentAnalysisStore`
   writes one ``analysis.json`` per rollup under
   ``<base>/<scope>-<key>/`` plus an atomically-rewritten
   ``index.json`` — the same pattern as ``app.style_analysis.storage``.
   ``default_comment_intel_dir()`` resolves ``<data_dir>/comment_intel/``.

2. DB seam — the comments store (PREFERRED, landed in Round 5 as
   ``app.comments.store.CommentsStore`` with migration 0006):
   :func:`persist_rollup_to_store` writes through the store's own
   ``create_analysis`` / ``create_moderation_flag`` API, so owner
   scoping, FK integrity and the schema CHECKs are all enforced by
   the store.  Spam flags are filed with ``flag_kind='spam'`` and the
   advisory ``suggested_action='review'`` (advisory only — flags
   never auto-execute; the engagement worker / a human decides).
   A flag whose comment is not in the store is counted as skipped,
   never silently dropped from the report.

3. Raw-connection fallback: :func:`persist_rollup_to_db` introspects
   a bare sqlite connection — if the ``comment_analyses`` /
   ``comment_moderation_flags`` tables exist it inserts filling
   whichever known columns the table actually has.  It NEVER creates
   those tables.  Both DB paths return a report dict naming exactly
   what was written where.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from pathlib import Path

from .schema import CommentRollup

__all__ = [
    "CommentAnalysisStore",
    "default_comment_intel_dir",
    "persist_rollup_to_store",
    "persist_rollup_to_db",
]


def persist_rollup_to_store(store, rollup: CommentRollup) -> dict:
    """Persist a rollup through ``app.comments.store.CommentsStore``.

    Writes one ``comment_analyses`` row (engine/model/provenance as
    first-class columns + the full rollup JSON) and one
    ``comment_moderation_flags`` row per spam flag.  Returns a report:
    ``{"store_analysis": bool, "store_moderation_flags": int,
    "flags_skipped": int, "error": str | None}`` — a store refusal
    (e.g. unknown channel) is reported, never hidden.
    """
    report: dict = {"store_analysis": False, "store_moderation_flags": 0,
                    "flags_skipped": 0, "error": None}
    if not rollup.channel_id:
        report["error"] = "rollup has no channel_id; the comments store is owner-scoped"
        return report
    try:
        store.create_analysis(
            rollup.channel_id,
            engine=rollup.provenance.engine,
            scope=rollup.scope,
            video_id=rollup.video_id,
            rollup=rollup.model_dump(mode="json"),
            model_id=rollup.provenance.model,
            comment_count=rollup.comment_count,
            provenance=rollup.provenance.model_dump(mode="json"),
        )
        report["store_analysis"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    provenance = rollup.provenance.model_dump(mode="json")
    for flag in rollup.spam_flags:
        try:
            store.create_moderation_flag(
                flag.comment_id,
                "spam",
                channel_id=rollup.channel_id,
                reason=flag.reason,
                suggested_action="review",
                provenance=provenance,
            )
            report["store_moderation_flags"] += 1
        except Exception:
            # Comment not stored / scope mismatch: counted, not hidden.
            report["flags_skipped"] += 1
    return report


def default_comment_intel_dir() -> Path:
    """``<data_dir>/comment_intel`` via the canonical resolver.

    Round 6: the previous fallback hand-rolled ``~/.local/share/
    YouTubeAgent`` — a DIFFERENT root from the rest of the app.  It now
    derives from :func:`app.data_dir.resolve_data_dir` (``YTA_DATA_DIR``
    override / Windows ``%APPDATA%\\YouTubeAgent`` / dev
    ``~/.youtube-agent``), matching the docstring's contract above.
    """
    from app.data_dir import resolve_data_dir

    return resolve_data_dir() / "comment_intel"


def _slug(value: str | None, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-")
    return s or fallback


class CommentAnalysisStore:
    """File-based store for comment rollups (latest analysis wins)."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _key(self, rollup: CommentRollup) -> str:
        if rollup.scope == "video":
            return f"video-{_slug(rollup.video_id, 'unknown')}"
        return f"channel-{_slug(rollup.channel_id, 'unknown')}"

    def _index_path(self) -> Path:
        return self.base / "index.json"

    def _load_index(self) -> dict:
        path = self._index_path()
        if not path.exists():
            return {"entries": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"entries": {}}

    def _write_index(self, index: dict) -> None:
        tmp = self._index_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
        os.replace(tmp, self._index_path())

    def save(self, rollup: CommentRollup) -> Path:
        key = self._key(rollup)
        folder = self.base / key
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "analysis.json"
        path.write_text(rollup.model_dump_json(indent=2), encoding="utf-8")
        index = self._load_index()
        index["entries"][key] = {
            "scope": rollup.scope,
            "video_id": rollup.video_id,
            "channel_id": rollup.channel_id,
            "comment_count": rollup.comment_count,
            "engine": rollup.provenance.engine,
            "model": rollup.provenance.model,
            "generated_at": rollup.provenance.generated_at,
            "path": str(path),
        }
        self._write_index(index)
        return path

    def load(self, *, scope: str, video_id: str | None = None,
             channel_id: str | None = None) -> CommentRollup | None:
        if scope == "video":
            key = f"video-{_slug(video_id, 'unknown')}"
        else:
            key = f"channel-{_slug(channel_id, 'unknown')}"
        path = self.base / key / "analysis.json"
        if not path.exists():
            return None
        return CommentRollup.model_validate_json(path.read_text(encoding="utf-8"))

    def list_entries(self) -> list[dict]:
        return list(self._load_index().get("entries", {}).values())


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str] | None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return None
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _insert_filtered(conn, table: str, columns: list[str], values: dict) -> bool:
    usable = {k: v for k, v in values.items() if k in columns and v is not None}
    # NOT NULL columns without defaults that we cannot fill -> skip honestly.
    notnull = {
        r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        if r[3] and r[4] is None and not r[5]
    }
    if not notnull.issubset(set(usable) | {"id"}):
        missing = notnull - set(usable) - {"id"}
        if missing and "id" not in columns:
            return False
        if missing - {"id"}:
            return False
    if "id" in columns and "id" not in usable:
        usable["id"] = uuid.uuid4().hex
    if not usable:
        return False
    cols = ", ".join(usable)
    placeholders = ", ".join("?" for _ in usable)
    try:
        conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
            list(usable.values()),
        )
    except sqlite3.IntegrityError:
        return False  # FK/CHECK refusal — caller reports it as not written
    return True


def persist_rollup_to_db(conn: sqlite3.Connection, rollup: CommentRollup) -> dict:
    """Write the rollup + spam flags into the schema worker's tables
    IF they exist.  Returns a report of exactly what happened."""
    report: dict = {"tables_present": [], "db_comment_analyses": False,
                    "db_moderation_flags": 0}
    payload = rollup.model_dump_json()
    now_ms = None
    try:
        from datetime import datetime, timezone

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    except Exception:
        pass

    analyses_cols = _table_columns(conn, "comment_analyses")
    if analyses_cols is not None:
        report["tables_present"].append("comment_analyses")
        values = {
            "video_id": rollup.video_id,
            "channel_id": rollup.channel_id,
            "scope": rollup.scope,
            "comment_count": rollup.comment_count,
            "analysed_count": rollup.analysed_count,
            "spam_count": rollup.spam_count,
            "engine": rollup.provenance.engine,
            "model": rollup.provenance.model,
            "model_id": rollup.provenance.model,
            "generated_at": rollup.provenance.generated_at,
            "analysis_json": payload,
            "payload_json": payload,
            "rollup_json": payload,
            "result_json": payload,
            "provenance_json": rollup.provenance.model_dump_json(),
            "created_at": now_ms,
            "updated_at": now_ms,
        }
        # Keep only ONE payload column (the first the table offers).
        for payload_col in ("analysis_json", "payload_json", "rollup_json", "result_json"):
            if payload_col in analyses_cols:
                for other in ("analysis_json", "payload_json", "rollup_json", "result_json"):
                    if other != payload_col:
                        values.pop(other, None)
                break
        report["db_comment_analyses"] = _insert_filtered(
            conn, "comment_analyses", analyses_cols, values
        )

    flags_cols = _table_columns(conn, "comment_moderation_flags")
    if flags_cols is not None:
        report["tables_present"].append("comment_moderation_flags")
        written = 0
        for flag in rollup.spam_flags:
            ok = _insert_filtered(
                conn, "comment_moderation_flags", flags_cols,
                {
                    "comment_id": flag.comment_id,
                    "video_id": flag.video_id,
                    "channel_id": flag.channel_id,
                    "reason": flag.reason,
                    "flag_kind": "spam",
                    "suggested_action": "review",
                    "provenance_json": rollup.provenance.model_dump_json(),
                    "status": "pending",
                    "source": "comment_intel",
                    "created_at": now_ms,
                    "updated_at": now_ms,
                },
            )
            written += 1 if ok else 0
        report["db_moderation_flags"] = written
    return report
