"""Adapters for the Learning Loop update engine.

SEAM STATUS (updated as siblings landed during this round)
===============================================================
* Learning Store: **LANDED, bridged.**
  ``app.learning.store.LearningStore`` (migration 0005) is the real
  SQLite store, but its method signatures differ from the documented
  protocol the engine was briefed against (scoped
  ``create_candidate(channel, niche, kind, ...)`` /
  ``activate_rule(id, channel_id=, niche=)`` /
  ``supersede_rule(...)``; ``effect_value`` instead of
  ``effect_estimate``; ``created_from`` is a list of provenance id
  strings, not a dict).  The sibling service layer
  (``app.learning.service._EngineStoreAdapter``) bridges the landed
  store to this engine's protocol, encoding the engine's dict-shaped
  ``created_from`` as one JSON string in the provenance list so
  idempotency round-trips.  :class:`InMemoryLearningRuleStore`
  below remains as the deterministic in-memory reference
  implementation of the engine-side protocol, for tests.
  EVIDENCE POLICY (UNIFIED — Round 5 closure, R5-005 / R5L-004):
  activation requires n>=3 EVERYWHERE — one shared constant
  (``app.learning.schema.MIN_EVIDENCE_FOR_ACTIVATION``, surfaced in
  the engine contract as ``MIN_EVIDENCE``), enforced by this
  engine, by the landed store's ``activate_rule`` itself (typed
  ``InsufficientEvidenceError``, no state change), and by
  :class:`InMemoryLearningRuleStore.activate` below.  The landed
  store additionally labels ``thin_evidence`` below n=5
  (``THIN_EVIDENCE_THRESHOLD``) — a DISPLAY band only: a rule with
  n=3–4 is legitimately active AND labelled thin; the label never
  blocks activation.
* Analytics: **PARTIAL REAL + documented gaps.**
  :class:`SqliteAnalyticsSource` reads the *real* tables
  ``analytics_snapshots`` JOIN ``projects`` LEFT JOIN
  ``niche_profiles`` (legacy metric columns, which the Round 5
  analytics store mirrors from ``metrics_json``).  It honestly
  supplies: views, watch hours, CTR, avg view duration, topic,
  target duration, niche key, publish time — and, since the
  calibration team landed, the **issued expectation band** from
  ``calibration_log`` (metric ``views``, per snapshot point).
  It still **cannot** supply ``hook_type`` / ``title_pattern`` /
  ``thumbnail_style`` (no project metadata columns record them) —
  always ``None`` here, so the SQLite source can only feed
  ``topic_affinity``, ``duration_band`` and ``publish_window``
  rules today.
  :class:`ListAnalyticsSource` wraps an in-memory list (tests /
  synthetic fixtures).
"""

from __future__ import annotations

import copy
import sqlite3
from typing import Any, Mapping, Optional, Sequence

from app.learning_loop.contracts import (
    MIN_EVIDENCE,
    SNAPSHOT_POINTS,
    ExpectationBand,
    VideoEvidence,
)

__all__ = [
    "InMemoryLearningRuleStore",
    "ListAnalyticsSource",
    "SqliteAnalyticsSource",
]

_DAY_MS = 86_400_000
#: Snapshot-point acceptance windows, days after publish (inclusive-low).
_POINT_WINDOWS_DAYS = {"D+7": (5, 14), "D+28": (21, 38)}


class InMemoryLearningRuleStore:
    """Reference implementation of the Learning Store contract.

    In-memory only (the seam stub).  Rules are never mutated except
    the single ``status -> superseded`` transition performed by
    :meth:`supersede` on the *old* rule, exactly as the contract
    specifies; callers receive deep copies so they cannot mutate
    stored rules either.  Ids and timestamps are a logical counter,
    keeping the store fully deterministic for tests.
    """

    def __init__(self) -> None:
        self._rules: list[dict] = []
        self._counter = 0

    def _next(self) -> tuple[str, int]:
        self._counter += 1
        return f"rule-{self._counter:04d}", self._counter * 1000

    @staticmethod
    def _check(fields: Mapping[str, Any]) -> None:
        for key in ("channel_id", "niche", "kind", "statement_family",
                    "statement", "effect_estimate", "evidence_count",
                    "confidence", "created_from"):
            if key not in fields:
                raise ValueError(f"learning rule field missing: {key}")

    def get_latest(self, channel_id, niche, kind, statement_family):
        candidates = [
            r for r in self._rules
            if r["channel_id"] == channel_id and r["niche"] == niche
            and r["kind"] == kind
            and r["statement_family"] == statement_family
            and r["status"] != "superseded"
        ]
        if not candidates:
            return None
        return copy.deepcopy(max(candidates, key=lambda r: r["version"]))

    def create_candidate(self, fields):
        self._check(fields)
        rule_id, ts = self._next()
        rule = {
            **copy.deepcopy(dict(fields)),
            "id": rule_id,
            "status": "candidate",
            "version": 1,
            "created_at": ts,
            "updated_at": ts,
        }
        self._rules.append(rule)
        return copy.deepcopy(rule)

    def activate(self, rule_id):
        for rule in self._rules:
            if rule["id"] == rule_id:
                if rule["status"] == "superseded":
                    raise ValueError("cannot activate a superseded rule")
                # Same unified gate as the real store (n>=3): the
                # reference implementation must not be a softer
                # back door around the activation threshold.
                if int(rule.get("evidence_count") or 0) < MIN_EVIDENCE:
                    raise ValueError(
                        f"insufficient evidence to activate rule "
                        f"{rule_id}: evidence_count="
                        f"{int(rule.get('evidence_count') or 0)} "
                        f"< {MIN_EVIDENCE} (activation refused, "
                        "no state change)"
                    )
                rule["status"] = "active"
                return copy.deepcopy(rule)
        raise KeyError(f"unknown learning rule {rule_id}")

    def supersede(self, old_rule_id, fields):
        self._check(fields)
        old = None
        for rule in self._rules:
            if rule["id"] == old_rule_id:
                old = rule
                break
        if old is None:
            raise KeyError(f"unknown learning rule {old_rule_id}")
        if old["status"] == "superseded":
            raise ValueError("rule already superseded")
        old["status"] = "superseded"  # the one permitted transition
        rule_id, ts = self._next()
        rule = {
            **copy.deepcopy(dict(fields)),
            "id": rule_id,
            "status": "candidate",
            "version": int(old["version"]) + 1,
            "supersedes_id": old_rule_id,
            "created_at": ts,
            "updated_at": ts,
        }
        self._rules.append(rule)
        return copy.deepcopy(rule)

    # -- test / inspection helpers (not part of the contract) -------
    def all_rules(self) -> list[dict]:
        return copy.deepcopy(self._rules)


class ListAnalyticsSource:
    """Analytics source over an in-memory list of evidence records."""

    def __init__(self, records: Sequence[VideoEvidence]) -> None:
        self._records = list(records)

    def fetch(self, snapshot_point, *, channel_id=None):
        if snapshot_point not in SNAPSHOT_POINTS:
            raise ValueError(f"unknown snapshot point {snapshot_point!r}")
        return [
            r for r in self._records
            if r.snapshot_point == snapshot_point
            and (channel_id is None or r.channel_id == channel_id)
        ]


class SqliteAnalyticsSource:
    """Partial real adapter over ``analytics_snapshots`` (see module
    docstring for exactly what it can and cannot supply)."""

    def __init__(self, conn_factory: Any) -> None:
        # Same convention as StyleLabStore: a bare connection or a
        # callable returning one.
        if hasattr(conn_factory, "execute"):
            self._shared = conn_factory
            self._factory = None
        elif callable(conn_factory):
            self._shared = None
            self._factory = conn_factory
        else:
            raise TypeError("conn_factory must be callable or a connection")

    def _connect(self):
        if self._shared is not None:
            return self._shared, False
        return self._factory(), True

    def _expectation_bands(
        self, conn, snapshot_point: str
    ) -> dict[str, ExpectationBand]:
        """Issued expectation bands for ``snapshot_point``.

        REAL SOURCE (landed by the sibling calibration team during
        this round): ``calibration_log`` — one row per (video,
        snapshot point, metric) with ``predicted_low`` /
        ``predicted_high`` recorded at publish time.  Views bands are
        keyed by project_id (and youtube_video_id as a fallback key).
        Returns {} when the table does not exist yet.
        """
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='calibration_log'"
        ).fetchone()
        if row is None:
            return {}
        bands: dict[str, ExpectationBand] = {}
        for rec in conn.execute(
            "SELECT project_id, youtube_video_id, predicted_low,"
            " predicted_high FROM calibration_log"
            " WHERE snapshot_point = ? AND metric = 'views'",
            (snapshot_point,),
        ):
            band = ExpectationBand(
                low=float(rec[2]), high=float(rec[3]), metric="views"
            )
            if rec[0]:
                bands[str(rec[0])] = band
            if rec[1]:
                bands.setdefault(str(rec[1]), band)
        return bands

    def fetch(self, snapshot_point, *, channel_id=None):
        if snapshot_point not in _POINT_WINDOWS_DAYS:
            raise ValueError(f"unknown snapshot point {snapshot_point!r}")
        low_d, high_d = _POINT_WINDOWS_DAYS[snapshot_point]
        conn, should_close = self._connect()
        try:
            bands = self._expectation_bands(conn, snapshot_point)
            sql = (
                "SELECT s.project_id, s.channel_id, s.youtube_video_id,"
                " s.snapshot_at, s.views, s.watch_hours_x100, s.ctr_bps,"
                " s.avg_view_duration_sec, p.topic, p.target_minutes,"
                " p.published_at, np.key"
                " FROM analytics_snapshots s"
                " JOIN projects p ON p.id = s.project_id"
                " LEFT JOIN niche_profiles np ON np.id = p.niche_profile_id"
                " WHERE p.published_at IS NOT NULL"
                " AND s.snapshot_at - p.published_at >= ?"
                " AND s.snapshot_at - p.published_at < ?"
            )
            params: list[Any] = [low_d * _DAY_MS, high_d * _DAY_MS]
            if channel_id is not None:
                sql += " AND s.channel_id = ?"
                params.append(str(channel_id))
            sql += " ORDER BY s.snapshot_at ASC"
            best: dict[str, VideoEvidence] = {}
            for rec in conn.execute(sql, params):
                (project_id, chan, yt_id, snap_at, views, watch_x100,
                 ctr_bps, avd, topic, target_min, published_at,
                 niche_key) = rec
                video_id = str(yt_id or project_id)
                # Later snapshot in the window wins (rows are ASC).
                best[video_id] = VideoEvidence(
                    video_id=video_id,
                    channel_id=str(chan),
                    niche=str(niche_key or "unknown"),
                    snapshot_point=snapshot_point,
                    published_at_ms=int(published_at),
                    snapshot_at_ms=int(snap_at),
                    views=int(views or 0),
                    watch_hours=float(watch_x100 or 0) / 100.0,
                    ctr=(float(ctr_bps) / 10_000.0) if ctr_bps is not None else None,
                    avg_view_duration_sec=(
                        float(avd) if avd is not None else None
                    ),
                    expectation_band=bands.get(str(project_id))
                    or bands.get(video_id),
                    topic=topic,
                    duration_sec=(
                        float(target_min) * 60.0 if target_min else None
                    ),
                    # hook_type / title_pattern / thumbnail_style:
                    # no source columns exist yet — see module docstring.
                )
            return [best[key] for key in sorted(best)]
        finally:
            if should_close:
                conn.close()
