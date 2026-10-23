"""Style Lab — background claimer for queued competitor-video analyses.

This module closes the intake -> analysis gap documented in
``docs/STYLE_LAB.md`` §7 ("As-built status of the claim loop"): the
REST endpoint ``POST .../competitor-videos/{video_id}/analysis`` flips
a ``competitor_videos`` row to ``analysis_status='queued'`` and stamps
``analysis_requested_at`` — and, until now, nothing ever claimed those
rows, so queued analyses stalled forever (bug SL-002).

The claimer implements §7 steps 2-4 exactly:

2. **Claim.** Rows with ``analysis_status='queued'`` are claimed
   oldest-request first and moved to ``'analysing'``.  §7 names
   ``StyleLabStore.set_analysis_status(video_id, "analysing",
   channel_id=<owner>)`` for this transition; that store method has
   **no expected-status guard** (it rewrites whatever status is
   current), so the claim itself is performed here as a
   *conditional* UPDATE — ``... WHERE id = ? AND
   analysis_status = 'queued'`` — and only proceeds when exactly one
   row changed, mirroring the status-guarded updates used by the
   sibling execution code (``app.execution.runner`` /
   ``app.queue``).  This keeps claiming safe against a double-claim
   even if a second poller is ever started by mistake.  The owner
   ``channel_id`` is always read from the competitor row and passed
   to the store on every later transition, so the store's scope check
   stays in force (§7 step 2).
3. **Success.** The analysis engine (``app.style_analysis.engine.
   analyse_video``) runs on the attached file (plus its thumbnail
   when one is stored) with ``metadata`` carrying
   ``competitor_video_id`` / ``channel_id`` / ``source_url``, and
   ``out_dir`` set to the conventional analysis store dir
   (``<data_dir>/stylelab``), so results land in the §6 layout —
   ``analysis.json`` + ``breakdown.md`` under the per-video directory
   plus the ``index.json`` entry, keyed by ``competitor_videos.id``
   and carrying the owner ``channel_id`` — persisted by the engine
   via ``app.style_analysis.storage``.  Only then is the row flipped to
   ``'analysed'`` via the DB store.
4. **Failure.** ANY exception — a missing/unreadable attached file
   (the pre-flight ``app.style_analysis.media.probe_media`` check raises
   the media module's typed :class:`MediaValidationError` with a
   machine-readable ``reason``: ``missing_file`` / ``ffprobe_failed``
   / ``no_video_stream`` / ``zero_duration`` / ...), a metadata-only
   row with no file to analyse (``no_attached_file``), an engine
   error, or a persistence failure — flips the row to
   ``'analysis_failed'`` via the DB store.  ``competitor_videos`` has
   **no error/reason column** (see migration 0004 / §2), so the
   reason is recorded durably in the analysis results layout, next
   to where a successful analysis would have landed:
   ``<data_dir>/stylelab/<slug of video id>/analysis_failure.json``
   (``{competitor_video_id, channel_id, competitor_id, status,
   reason, error_class, detail, failed_at}``, written atomically),
   and logged at ERROR level.  Exactly ONE attempt is made per claim:
   no retry loop, no crash loop, no silent stall — re-queueing stays
   a user action through the existing endpoint, exactly as the
   failure contract requires.  A failed row never blocks the queue:
   the poll continues with the next queued row.

Control flags / lifecycle
-------------------------
The claimer is NOT a standalone runtime.  It is owned by
:class:`app.execution.service.ExecutionService`, which runs
:meth:`CompetitorAnalysisClaimer.poll_once` on a poll loop in a
worker thread (the engine's ffmpeg/Whisper work is blocking) next to
the JobRunner and scheduler loops, and cancels that loop on
:meth:`ExecutionService.stop` (app shutdown).  ``poll_once`` honours
the same persisted control flags the JobRunner honours
(:mod:`app.execution.flags`, re-read before *every* claim):
``stop_all`` and ``pause_all`` both mean "claim nothing new";
``stop_all`` additionally latches :attr:`stop_requested` so the
hosting loop exits, exactly like the runner's loop does.

Scope discipline: the poller scans *all* channels' queues (it is a
single process-wide worker), but each run touches only the claimed
video's own DB row (owner-checked transitions) and its own per-video
analysis directory / index entry.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.logging_setup import get_logger

logger = get_logger("app.style_analysis.claimer")

__all__ = [
    "CompetitorAnalysisClaimer",
    "ClaimerError",
    "FAILURE_FILENAME",
    "REASON_NO_ATTACHED_FILE",
    "REASON_PERSISTENCE_FAILED",
]

#: File (inside the per-video analysis dir) recording a failure reason.
FAILURE_FILENAME = "analysis_failure.json"

#: Machine-readable failure reasons owned by this module.  Media
#: failures reuse ``app.style_analysis.media``'s REASON_* vocabulary.
REASON_NO_ATTACHED_FILE = "no_attached_file"
REASON_PERSISTENCE_FAILED = "analysis_persistence_failed"

_MAX_DETAIL_CHARS = 500


class ClaimerError(Exception):
    """An analysis failure with a machine-readable ``reason``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def _now_ms() -> int:
    return int(time.time() * 1000)


def _reason_for(exc: BaseException) -> tuple[str, str]:
    """(machine-readable reason, human detail) for one failure.

    The media module's typed errors (and this module's own
    :class:`ClaimerError`) carry ``reason``; anything else falls back
    to the exception class name — the same honesty convention the
    engine's stage provenance uses (class name + truncated message,
    never a traceback, never a secret).
    """
    reason = (
        getattr(exc, "reason", None)
        or getattr(exc, "code", None)
        or type(exc).__name__
    )
    detail = str(exc) or type(exc).__name__
    return str(reason), detail[:_MAX_DETAIL_CHARS]


class CompetitorAnalysisClaimer:
    """Claims ``'queued'`` competitor videos and runs the engine on them.

    Parameters
    ----------
    conn_factory:
        Zero-arg callable returning a fresh ``sqlite3.Connection``
        (the same contract the DB store, queue and runner use).
    data_dir:
        The app data dir (``Settings.data_dir``).  Attached files are
        resolved as ``data_dir / storage_path`` and analysis results
        land under ``data_dir / stylelab``.  When omitted it is
        resolved lazily per poll (settings -> ``YTA_DATA_DIR`` -> the
        DB file's parent), mirroring ``app.style_lab.service``.
    analyse_fn:
        The engine entry point; defaults to
        ``app.style_analysis.engine.analyse_video`` resolved at call time.
        Injectable for tests.
    max_per_poll:
        Upper bound on rows claimed per :meth:`poll_once` call, so one
        poll can never monopolise the worker thread forever.
    """

    def __init__(
        self,
        conn_factory: Callable,
        *,
        data_dir: Path | str | None = None,
        analyse_fn: Optional[Callable] = None,
        max_per_poll: int = 10,
    ) -> None:
        if not callable(conn_factory):
            raise TypeError("conn_factory must be callable")
        self._conn_factory = conn_factory
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._analyse_fn = analyse_fn
        self._max_per_poll = max(1, int(max_per_poll))
        #: Latched when a poll observes the persisted ``stop_all``
        #: flag; the hosting service loop exits when it sees this.
        self.stop_requested = False

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _resolve_data_dir(self) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        try:
            from app.config import get_settings

            return Path(get_settings().data_dir)
        except Exception:  # noqa: BLE001 - fall back like the intake service
            from app.data_dir import resolve_data_dir

            return resolve_data_dir()

    def _db_store(self):
        """The intake DB store (``app.style_lab.store`` — underscore)."""
        from app.style_lab.store import StyleLabStore

        return StyleLabStore(self._conn_factory)

    def _flags(self):
        try:
            from app.execution.flags import read_flags

            return read_flags(self._conn_factory)
        except Exception:  # noqa: BLE001 - a broken flag read must not stall
            from app.execution.flags import Flags

            return Flags()

    def stop_all_active(self) -> bool:
        """True when the persisted STOP ALL flag is currently set."""
        return bool(self._flags().stop_all)

    def _claiming_blocked(self) -> bool:
        """True when control flags forbid claiming (STOP ALL / pause)."""
        flags = self._flags()
        if flags.stop_all:
            self.stop_requested = True
        return bool(flags.stop_all or flags.pause_all)

    # ------------------------------------------------------------------
    # queue scan + conditional claim (§7 step 2)
    # ------------------------------------------------------------------
    def list_queued(self, limit: Optional[int] = None) -> list[dict]:
        """Queued rows across ALL channels, oldest request first.

        Each row is the ``competitor_videos`` dict plus
        ``channel_id`` — the owner channel taken from its competitor
        row (the §7 claim must always pass that owner to the store).
        """
        from app.style_lab.schema import ensure_style_lab_schema

        conn = self._conn_factory()
        try:
            conn.row_factory = sqlite3.Row
            ensure_style_lab_schema(conn)
            sql = (
                "SELECT v.*, c.channel_id AS owner_channel_id"
                " FROM competitor_videos v"
                " JOIN competitor_channels c"
                "   ON c.id = v.competitor_channel_id"
                " WHERE v.analysis_status = 'queued'"
                " ORDER BY v.analysis_requested_at IS NULL,"
                "          v.analysis_requested_at ASC,"
                "          v.created_at ASC, v.rowid ASC"
            )
            params: tuple = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (int(limit),)
            rows = conn.execute(sql, params).fetchall()
            queued = []
            for row in rows:
                data = dict(row)
                data["channel_id"] = data.pop("owner_channel_id")
                tags = data.get("tags_json")
                if isinstance(tags, str):
                    try:
                        parsed = json.loads(tags)
                    except ValueError:
                        parsed = []
                    data["tags"] = parsed if isinstance(parsed, list) else []
                queued.append(data)
            return queued
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def claim(self, video: dict) -> Optional[dict]:
        """Conditionally claim one queued row: ``queued -> analysing``.

        The UPDATE is guarded on the row still being ``'queued'``;
        when the guard matches nothing (already claimed / status moved
        by someone else) ``None`` is returned and the row is left
        alone — a double-claim is impossible.  On success the fresh
        row (status ``'analysing'``) is read back through the DB store
        with the owner scope check applied.
        """
        video_id = str(video["id"])
        channel_id = str(video["channel_id"])
        from app.style_lab.schema import ensure_style_lab_schema

        conn = self._conn_factory()
        try:
            ensure_style_lab_schema(conn)
            cur = conn.execute(
                "UPDATE competitor_videos SET analysis_status = 'analysing',"
                " updated_at = ? WHERE id = ? AND analysis_status = 'queued'",
                (_now_ms(), video_id),
            )
            try:
                conn.commit()
            except Exception:
                pass
            if (cur.rowcount or 0) != 1:
                return None
        finally:
            try:
                conn.close()
            except Exception:
                pass
        claimed = self._db_store().get_video(video_id, channel_id=channel_id)
        if claimed is None:  # pragma: no cover - row vanished mid-claim
            return None
        claimed["channel_id"] = channel_id
        return claimed

    def claim_next(self) -> Optional[dict]:
        """Claim the oldest queued row, or ``None`` when none/blocked."""
        if self._claiming_blocked():
            return None
        for candidate in self.list_queued(limit=self._max_per_poll):
            claimed = self.claim(candidate)
            if claimed is not None:
                return claimed
        return None

    # ------------------------------------------------------------------
    # one analysis run (§7 steps 3-4)
    # ------------------------------------------------------------------
    def _resolve_under_data_dir(self, data_dir: Path, rel: Any) -> Optional[Path]:
        if not rel:
            return None
        path = Path(str(rel))
        return path if path.is_absolute() else data_dir / path

    def _record_failure(self, video: dict, reason: str, detail: str,
                        error_class: Optional[str]) -> Optional[Path]:
        """Persist the failure reason in the §6 analysis layout.

        Writes ``<data_dir>/stylelab/<slug>/analysis_failure.json``
        atomically (tmp file + ``os.replace``, like the analysis
        store's index writer).  Best-effort: a recording failure is
        logged and never masks the status flip that follows.
        """
        try:
            from app.style_analysis.storage import default_stylelab_dir, slugify_key

            base = default_stylelab_dir(self._resolve_data_dir())
            vdir = base / slugify_key(str(video["id"]))
            vdir.mkdir(parents=True, exist_ok=True)
            payload = {
                "competitor_video_id": str(video["id"]),
                "channel_id": video.get("channel_id"),
                "competitor_id": video.get("competitor_channel_id"),
                "status": "analysis_failed",
                "reason": reason,
                "error_class": error_class,
                "detail": detail,
                "failed_at": _now_ms(),
                "analysis_requested_at": video.get("analysis_requested_at"),
            }
            path = vdir / FAILURE_FILENAME
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return path
        except Exception as exc:  # noqa: BLE001 - recording is best-effort
            logger.warning(
                "stylelab claimer: could not record failure for video %s: %s",
                video.get("id"), exc,
            )
            return None

    def _clear_failure_record(self, video_id: str) -> None:
        """Drop a stale failure record after a successful re-analysis."""
        try:
            from app.style_analysis.storage import default_stylelab_dir, slugify_key

            base = default_stylelab_dir(self._resolve_data_dir())
            path = base / slugify_key(str(video_id)) / FAILURE_FILENAME
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            pass

    def _run_analysis(self, video: dict) -> dict:
        """Run the engine for one claimed row; return a success report.

        Raises on ANY failure — the caller (:meth:`process`) owns the
        §7 step-4 transition.  Blocking (ffprobe/ffmpeg/Whisper), so
        the hosting service always calls this via a worker thread.
        """
        data_dir = self._resolve_data_dir()
        video_path = self._resolve_under_data_dir(
            data_dir, video.get("storage_path"))
        if video_path is None or video.get("source") != "attached_file":
            raise ClaimerError(
                REASON_NO_ATTACHED_FILE,
                "competitor video has no attached file to analyse "
                f"(source={video.get('source')!r})",
            )
        # Pre-flight: the file must still probe as a real video.  The
        # media module's typed error carries the machine-readable
        # reason (missing_file / ffprobe_failed / no_video_stream /
        # zero_duration) that the failure record persists.
        from app.style_analysis.media import probe_media

        probe_media(video_path)

        thumbnail_path = self._resolve_under_data_dir(
            data_dir, video.get("thumbnail_storage_path"))
        if thumbnail_path is not None and not thumbnail_path.is_file():
            thumbnail_path = None

        from app.style_analysis.storage import default_stylelab_dir

        out_dir = default_stylelab_dir(data_dir)
        metadata = {
            "competitor_video_id": str(video["id"]),
            "channel_id": video.get("channel_id"),
            "source_url": video.get("video_url"),
            "title": video.get("title"),
        }
        analyse = self._analyse_fn
        if analyse is None:
            from app.style_analysis.engine import analyse_video

            analyse = analyse_video
        result = analyse(
            video_path,
            thumbnail_path=thumbnail_path,
            metadata=metadata,
            out_dir=out_dir,
        )
        if result is None:
            raise ClaimerError(
                REASON_PERSISTENCE_FAILED, "analysis engine returned no result"
            )
        # The engine degrades per-stage instead of raising, and its
        # persistence step reports (never raises) its own outcome —
        # a failed persistence is the one result-level failure: the
        # §6 outputs simply do not exist, so claiming 'analysed'
        # would be a lie.
        payload: dict = {}
        try:
            from app.style_analysis.engine import result_payload

            payload = result_payload(result)
        except Exception:  # noqa: BLE001 - payload is advisory here
            payload = {}
        persistence = payload.get("persistence")
        if isinstance(persistence, dict) and persistence.get("via") == "failed":
            error = persistence.get("error") or {}
            raise ClaimerError(
                REASON_PERSISTENCE_FAILED,
                f"analysis results could not be persisted: "
                f"{error.get('error_class')}: {error.get('error_message')}",
            )
        return {
            "video_id": str(video["id"]),
            "channel_id": video.get("channel_id"),
            "status": "analysed",
            "persistence_via": (
                persistence.get("via") if isinstance(persistence, dict) else None
            ),
        }

    def process(self, video: dict) -> dict:
        """Analyse one CLAIMED row and settle its status (§7 steps 3-4).

        Exactly one attempt.  Never raises: every outcome — success,
        engine/media failure, even a failure of the failure-recording
        itself — is converted into a report dict, and the row always
        ends in ``'analysed'`` or ``'analysis_failed'`` so nothing is
        left stalled in ``'analysing'`` by this run.
        """
        video_id = str(video["id"])
        channel_id = video.get("channel_id")
        store = self._db_store()
        try:
            report = self._run_analysis(video)
        except Exception as exc:  # noqa: BLE001 - §7 step 4: ANY exception
            reason, detail = _reason_for(exc)
            logger.error(
                "stylelab analysis failed for competitor video %s "
                "(channel %s): %s: %s",
                video_id, channel_id, reason, detail,
            )
            failure_path = self._record_failure(
                video, reason, detail, type(exc).__name__)
            try:
                store.set_analysis_status(
                    video_id, "analysis_failed", channel_id=channel_id)
            except Exception as flip_exc:  # noqa: BLE001 - logged, not hidden
                logger.error(
                    "stylelab claimer: could not flip video %s to "
                    "analysis_failed: %s", video_id, flip_exc,
                )
            return {
                "video_id": video_id,
                "channel_id": channel_id,
                "status": "analysis_failed",
                "reason": reason,
                "detail": detail,
                "failure_record": str(failure_path) if failure_path else None,
            }
        self._clear_failure_record(video_id)
        try:
            store.set_analysis_status(video_id, "analysed", channel_id=channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "stylelab claimer: analysis for video %s succeeded but "
                "the 'analysed' flip failed: %s", video_id, exc,
            )
            return {
                "video_id": video_id,
                "channel_id": channel_id,
                "status": "analysed_unrecorded",
                "reason": type(exc).__name__,
                "detail": str(exc)[:_MAX_DETAIL_CHARS],
            }
        logger.info(
            "stylelab analysis complete for competitor video %s (channel %s)",
            video_id, channel_id,
        )
        return report

    # ------------------------------------------------------------------
    # the poll (driven by ExecutionService's loop / directly by tests)
    # ------------------------------------------------------------------
    def poll_once(self) -> list[dict]:
        """Claim + process queued rows until the queue drains / caps.

        Flags are re-read before EVERY claim (the runner's pattern):
        with ``stop_all`` or ``pause_all`` active nothing new is
        claimed.  Returns one report dict per processed row; an empty
        list means nothing was claimed (empty queue or flags set).
        """
        reports: list[dict] = []
        while len(reports) < self._max_per_poll:
            if self._claiming_blocked():
                break
            claimed = self.claim_next()
            if claimed is None:
                break
            reports.append(self.process(claimed))
        return reports
