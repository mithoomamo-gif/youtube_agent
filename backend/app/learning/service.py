"""Learning Loop REST service — the layer ``app.routers.learning``
talks to.

Scope: the API surface of the Round 5 learning loop ONLY (reads +
one explicit trigger).  Learning stays bounded (MASTER_ROADMAP):
rules are versioned, evidence-counted and scoped per
``(channel_id, niche)``; nothing here rewrites a prompt, and nothing
here runs on a schedule — the batch evaluation runs only when the
user invokes the trigger endpoint.

Sibling consumption (who owns what)
-----------------------------------
* **Rules / history** — LANDED: :class:`app.learning.store.LearningStore`
  over the ``learning_rules`` table (migration 0005).  Consumed
  directly.  The store's ``is_thin_evidence`` label is surfaced
  verbatim; thin-evidence rules are listed, never hidden.
* **Rule history ordering** — the store returns family/version
  ascending (oldest first); this service re-sorts newest first, as
  the API contract requires.
* **Calibration summary** — LANDED: :func:`app.calibration.report.
  build_calibration_report` over the ``calibration_log`` table
  (migration 0007).  Consumed directly: per metric scope, the D+7 /
  D+28 points carry ``signed_bias_pct`` / ``mape_pct`` /
  ``band_coverage`` and the sibling's own insufficient-evidence
  labelling (``evidence='insufficient_evidence'``, aggregates None
  below its ``MIN_EVIDENCE``).  When a (channel, niche) has no
  calibration rows at all the report has no scopes — this service
  still answers with an explicit top-level insufficient-evidence
  label rather than an empty-looking success.
* **Batch evaluation trigger** — LANDED: :class:`app.learning_loop.
  engine.UpdateEngine` fed by :class:`app.learning_loop.adapters.
  SqliteAnalyticsSource`.  SEAM (flagged for the coordinator): the
  engine codes against the *documented* store protocol in
  :mod:`app.learning_loop.contracts` (``get_latest`` /
  ``create_candidate(fields)`` / ``activate(id)`` /
  ``supersede(id, fields)``, ``effect_estimate``, dict-shaped
  ``created_from``), while the landed :class:`LearningStore` exposes
  scoped methods (``create_candidate(channel, niche, kind, ...)``,
  ``activate_rule(id, channel_id=, niche=)``, ``effect_value``, list
  ``created_from`` provenance).  :class:`_EngineStoreAdapter` below
  bridges the two — including encoding the engine's dict-shaped
  ``created_from`` as a single JSON string inside the store's
  provenance list so the engine's idempotency check round-trips.
  The analytics source is additionally wrapped so a trigger for one
  niche can only learn rules for that niche (the source itself
  filters by channel only).
* **Production lessons** — LANDED while this service was being
  written: :func:`app.learning.lessons.production_lessons_for_scope`
  (aggregates ``approvals`` rows with ``decision='rejected'`` per
  (channel, niche) into ``lessons`` / ``observations`` split at its
  ``LESSON_THRESHOLD``).  Consumed directly with its real
  signature; the sibling router exposes it at
  ``GET .../learning/production-lessons``.  This service's
  :func:`get_production_lessons` consumes the same function (and
  mirrors the router in dropping the redundant ``entries`` list).
  Retained fallback (owned here, clearly labelled
  ``source='projects_gate_aggregation'``): an honest aggregation
  over real production records in scope — gate rejections from
  ``projects.gate_status_json`` (gate_a / gate_b) and failed /
  blocked projects with their ``error_summary`` as reject-reason
  lessons, plus per-status project counts as observations.  Scope
  joins ``projects.niche_profile_id -> niche_profiles`` and matches
  the niche by profile key OR id (the learning scope convention).
* **Publish-window suggestion** — LANDED (R5L-003 closure):
  :func:`app.publish_window.get_publish_window_suggestion` builds
  the real :class:`app.publish_window.PublishWindowService` over
  the SQLite analytics adapter, and the seam below consumes it —
  its eligibility structure (the roadmap gate: ≥10 published
  videos AND ≥28 days of analytics) is returned VERBATIM (never
  flattened into a fake suggestion).  The seam tries the first
  importable of ``app.learning.publish_window`` /
  ``app.learning_loop.publish_window`` / ``app.publish_window``
  exposing ``get_publish_window_suggestion(channel_id)``.  The
  rule-derived fallback below is retained ONLY for builds where
  no sibling import succeeds at all; with the real package landed
  it is unreachable in practice.

Owner scoping / isolation posture
---------------------------------
Every operation first resolves the owner channel (``channels.id``;
a YouTube channel id is also accepted, mirroring
``app.routers.channels`` / ``app.style_lab.service``).  Unknown
owner -> 404 ``channel_not_found``.  All reads are scoped by the
resolved channel (and niche), so another channel's learning data is
never returned — the house rule (404 / empty, never 403, never a
leak).
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import logging
from typing import Any, Optional

from app.errors import ApiError
from app.learning.schema import RULE_KINDS
from app.learning.store import LearningStore

logger = logging.getLogger(__name__)

__all__ = [
    "resolve_channel_id",
    "get_rules",
    "get_rule_history",
    "get_calibration_summary",
    "get_production_lessons",
    "get_publish_window_suggestion",
    "trigger_learning_batch",
    "SNAPSHOT_POINTS",
]

#: Grading points the update engine evaluates (engine contract).
SNAPSHOT_POINTS = ("D+7", "D+28")

#: Fallback lessons: fewer occurrences than this is thin evidence.
LESSON_THIN_THRESHOLD = 3

#: Fallback publish-window eligibility: at least one ACTIVE learned
#: publish_window rule for the channel.
PUBLISH_WINDOW_MIN_ACTIVE_RULES = 1


# ---------------------------------------------------------------------------
# Connection plumbing + owner-channel resolution
# ---------------------------------------------------------------------------


def _conn_factory():
    from app import db as db_module

    return db_module.get_connection


def _new_conn():
    from app import db as db_module

    return db_module.get_connection()


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def resolve_channel_id(channel_id: str) -> str:
    """Resolve an owner channel reference to its internal
    ``channels.id`` (internal id or YouTube channel id).  Raises 404
    ``channel_not_found`` when no live channel matches — every
    learning operation starts here."""
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


def _require_niche(niche: Any) -> str:
    """Niche is a REQUIRED scope parameter: present (router) and
    non-blank (here).  Rules are never pooled across niches, so an
    unscoped read is refused, never defaulted."""
    value = str(niche).strip() if niche is not None else ""
    if not value:
        raise ApiError(
            422,
            "validation_error",
            "A niche is required: learning rules are scoped per "
            "(channel, niche) and are never pooled across niches.",
            details={"field": "niche"},
        )
    return value


def _store() -> LearningStore:
    return LearningStore(_conn_factory())


# ---------------------------------------------------------------------------
# Sibling seam loader (lessons / publish-window): re-imported on every
# call so a sibling landing later — or a test double — is picked up.
# ---------------------------------------------------------------------------


def _sibling_fn(candidates: list[tuple[str, str]]):
    """First importable ``(module, attribute)`` callable, or None."""
    for module_name, attr in candidates:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - sibling not landed yet
            continue
        fn = getattr(module, attr, None)
        if callable(fn):
            return fn
    return None


# ---------------------------------------------------------------------------
# 1. Rules (active + candidate) for (channel, niche)
# ---------------------------------------------------------------------------


def get_rules(channel_id: str, niche: Any) -> dict:
    """Active + candidate rules for exactly (channel, niche).

    Thin-evidence rules are included and carry the store's
    ``is_thin_evidence`` label — labelled, never hidden.  Superseded
    rules are history (see :func:`get_rule_history`), not current
    rules, so they are not listed here.
    """
    channel_pk = resolve_channel_id(channel_id)
    niche_s = _require_niche(niche)
    store = _store()
    try:
        active = store.list_active_rules(channel_pk, niche_s)
        candidates = store.list_candidate_rules(channel_pk, niche_s)
    except ValueError as exc:
        raise ApiError(
            422, "validation_error", str(exc), details={"field": "niche"}
        ) from exc
    items = active + candidates
    return {
        "channel_id": channel_pk,
        "niche": niche_s,
        "items": items,
        "counts": {
            "active": len(active),
            "candidate": len(candidates),
            "thin_evidence": sum(
                1 for rule in items if rule.get("is_thin_evidence")
            ),
        },
        "next_cursor": None,
    }


# ---------------------------------------------------------------------------
# 2. Rule history for (channel, niche, kind) — superseded included,
#    newest first
# ---------------------------------------------------------------------------


def get_rule_history(channel_id: str, niche: Any, kind: Any) -> dict:
    """Full version history of one rule kind in scope.

    Includes superseded versions (the audit chain) and is ordered
    newest first (``created_at`` desc, then ``version`` desc) — the
    store's own family/version-ascending order is re-sorted here to
    the API contract.
    """
    channel_pk = resolve_channel_id(channel_id)
    niche_s = _require_niche(niche)
    kind_s = str(kind).strip() if kind is not None else ""
    if not kind_s:
        raise ApiError(
            422,
            "validation_error",
            "A rule kind is required for history.",
            details={"field": "kind", "allowed": list(RULE_KINDS)},
        )
    store = _store()
    try:
        rows = store.list_rule_history(channel_pk, niche_s, kind=kind_s)
    except ValueError as exc:
        raise ApiError(
            422,
            "validation_error",
            str(exc),
            details={"field": "kind", "allowed": list(RULE_KINDS)},
        ) from exc
    rows = sorted(
        rows,
        key=lambda r: (r.get("created_at") or 0, r.get("version") or 0),
        reverse=True,
    )
    return {
        "channel_id": channel_pk,
        "niche": niche_s,
        "kind": kind_s,
        "items": rows,
        "next_cursor": None,
    }


# ---------------------------------------------------------------------------
# 3. Calibration summary for (channel, niche) — calibration sibling's
#    read model (app.calibration.report.build_calibration_report)
# ---------------------------------------------------------------------------


def get_calibration_summary(channel_id: str, niche: Any) -> dict:
    """D+7 / D+28 bias / MAPE / band-coverage for (channel, niche).

    Consumes the calibration sibling's read model verbatim per
    metric scope; adds only the resolved scope echo and a top-level
    evidence roll-up.  A scope with no rows at all answers with the
    explicit insufficient-evidence label — never bare emptiness.
    """
    channel_pk = resolve_channel_id(channel_id)
    niche_s = _require_niche(niche)
    try:
        from app.calibration.report import build_calibration_report
    except Exception as exc:  # noqa: BLE001 - sibling missing/broken
        logger.warning("calibration report module unavailable: %s", exc)
        return {
            "channel_id": channel_pk,
            "niche": niche_s,
            "generated_at": None,
            "scopes": [],
            "evidence": "insufficient_evidence",
            "label": "insufficient evidence",
            "reason": "calibration_module_unavailable",
        }
    report = build_calibration_report(
        _conn_factory(), channel_id=channel_pk, niche=niche_s
    )
    scopes = report.get("scopes") or []
    any_sufficient = any(
        (point or {}).get("evidence") == "sufficient"
        for scope in scopes
        for point in (scope.get("points") or {}).values()
    )
    result = {
        "channel_id": channel_pk,
        "niche": niche_s,
        "generated_at": report.get("generated_at"),
        "scopes": scopes,
        "evidence": "sufficient" if any_sufficient else "insufficient_evidence",
    }
    if not any_sufficient:
        result["label"] = "insufficient evidence"
        if not scopes:
            result["reason"] = "no_calibration_rows"
    return result


# ---------------------------------------------------------------------------
# 4. Production lessons for (channel, niche) — consumption sibling
#    seam + owned fallback aggregation over real production records
# ---------------------------------------------------------------------------

_LESSON_SIBLINGS = [
    ("app.learning.lessons", "get_production_lessons"),
    ("app.learning.lessons", "aggregate_production_lessons"),
    ("app.learning.consumption", "get_production_lessons"),
    ("app.learning.consumption", "aggregate_production_lessons"),
    ("app.production_lessons", "get_production_lessons"),
]


def get_production_lessons(channel_id: str, niche: Any) -> dict:
    """Aggregated reject-reason lessons + observations in scope.

    Sibling structure is returned verbatim when the consumption
    sibling has landed; otherwise the fallback aggregation below
    runs and says so via ``source``.
    """
    channel_pk = resolve_channel_id(channel_id)
    niche_s = _require_niche(niche)
    # Landed sibling (real signature): production_lessons_for_scope(
    # source, *, channel_id, niche) over a conn_factory.
    try:
        from app.learning.lessons import production_lessons_for_scope
    except Exception:  # noqa: BLE001 - sibling not landed
        production_lessons_for_scope = None
    if production_lessons_for_scope is not None:
        view = production_lessons_for_scope(
            _conn_factory(), channel_id=channel_pk, niche=niche_s
        )
        view = dict(view)
        view.pop("entries", None)  # mirror the sibling router
        return view
    fn = _sibling_fn(_LESSON_SIBLINGS)
    if fn is not None:
        result = fn(channel_pk, niche_s)
        if isinstance(result, dict):
            return result
        return {
            "channel_id": channel_pk,
            "niche": niche_s,
            "items": list(result),
            "next_cursor": None,
        }
    return _fallback_production_lessons(channel_pk, niche_s)


def _gate_decision(value: Any) -> tuple[Optional[str], Optional[str]]:
    """(decision, reason) from one gate_status_json entry — entries
    are stored either as a bare decision string or as an object."""
    if isinstance(value, str):
        return value.strip().lower(), None
    if isinstance(value, dict):
        decision = value.get("decision") or value.get("status")
        reason = value.get("reason") or value.get("detail")
        return (
            str(decision).strip().lower() if decision else None,
            str(reason) if reason else None,
        )
    return None, None


def _fallback_production_lessons(channel_pk: str, niche_s: str) -> dict:
    """Aggregate REAL production records for (channel, niche).

    Reject-reason lessons: gate_a / gate_b rejections recorded on
    ``projects.gate_status_json`` and projects that ended
    ``failed`` / ``blocked`` (with their recorded error summary).
    Observations: per-status project counts in scope.  Every lesson
    carries its occurrence count as its evidence count and the
    thin-evidence label below :data:`LESSON_THIN_THRESHOLD`.
    """
    conn = _new_conn()
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "projects" not in tables or "niche_profiles" not in tables:
            rows: list = []
        else:
            rows = conn.execute(
                "SELECT p.id, p.status, p.error_summary,"
                " p.gate_status_json FROM projects p"
                " JOIN niche_profiles np ON np.id = p.niche_profile_id"
                " WHERE p.channel_id = ? AND (np.key = ? OR np.id = ?)"
                " AND p.deleted_at IS NULL",
                (channel_pk, niche_s, niche_s),
            ).fetchall()
    finally:
        _close(conn)

    reject: dict[tuple[str, str], dict] = {}
    status_counts: dict[str, int] = {}

    def _bump(bucket: dict, key: tuple[str, str], project_id: str,
              detail: Optional[str]) -> None:
        entry = bucket.setdefault(
            key,
            {
                "lesson_kind": "reject_reason",
                "reason_code": key[1],
                "gate": key[0],
                "occurrences": 0,
                "project_ids": [],
                "details": [],
            },
        )
        entry["occurrences"] += 1
        if len(entry["project_ids"]) < 5:
            entry["project_ids"].append(str(project_id))
        if detail and detail not in entry["details"] and len(entry["details"]) < 3:
            entry["details"].append(detail)

    for row in rows:
        project_id, status, error_summary, gate_raw = row[0], row[1], row[2], row[3]
        status_counts[str(status)] = status_counts.get(str(status), 0) + 1
        gates: Any = None
        if gate_raw:
            try:
                gates = json.loads(gate_raw)
            except (ValueError, TypeError):
                gates = None
        if isinstance(gates, dict):
            for gate_key in ("gate_a", "gate_b"):
                if gate_key not in gates:
                    continue
                decision, reason = _gate_decision(gates[gate_key])
                if decision in ("rejected", "reject", "failed", "fail", "denied"):
                    _bump(
                        reject,
                        (gate_key, reason or f"{gate_key}_rejected"),
                        project_id,
                        reason,
                    )
        if status in ("failed", "blocked"):
            _bump(
                reject,
                ("production", f"project_{status}"),
                project_id,
                str(error_summary) if error_summary else None,
            )

    items: list[dict] = []
    for entry in reject.values():
        entry["evidence_count"] = entry["occurrences"]
        entry["is_thin_evidence"] = entry["occurrences"] < LESSON_THIN_THRESHOLD
        entry["evidence_label"] = (
            "thin_evidence" if entry["is_thin_evidence"] else "sufficient"
        )
        items.append(entry)
    for status, count in sorted(status_counts.items()):
        items.append(
            {
                "lesson_kind": "observation",
                "reason_code": f"projects_{status}",
                "gate": None,
                "occurrences": count,
                "evidence_count": count,
                "is_thin_evidence": count < LESSON_THIN_THRESHOLD,
                "evidence_label": (
                    "thin_evidence" if count < LESSON_THIN_THRESHOLD
                    else "sufficient"
                ),
            }
        )
    items.sort(key=lambda e: (-e["occurrences"], e["reason_code"]))
    return {
        "channel_id": channel_pk,
        "niche": niche_s,
        "source": "projects_gate_aggregation",
        "items": items,
        "next_cursor": None,
    }


# ---------------------------------------------------------------------------
# 5. Publish-window suggestion for a channel — publish-window sibling
#    seam + owned fallback over learned publish_window rules
# ---------------------------------------------------------------------------

_PUBLISH_WINDOW_SIBLINGS = [
    ("app.learning.publish_window", "get_publish_window_suggestion"),
    ("app.learning_loop.publish_window", "get_publish_window_suggestion"),
    ("app.publish_window", "get_publish_window_suggestion"),
]


def get_publish_window_suggestion(channel_id: str) -> dict:
    """Publish-window suggestion for the channel.

    The sibling's eligibility structure is returned VERBATIM when
    the sibling has landed — including its not-enough-data form.
    The fallback derives eligibility from the channel's own learned
    ``publish_window`` rules (see module docstring).
    """
    channel_pk = resolve_channel_id(channel_id)
    fn = _sibling_fn(_PUBLISH_WINDOW_SIBLINGS)
    if fn is not None:
        result = fn(channel_pk)
        if isinstance(result, dict):
            return result
        return {"channel_id": channel_pk, "suggestion": result}
    return _fallback_publish_window(channel_pk)


def _fallback_publish_window(channel_pk: str) -> dict:
    from app.learning.schema import ensure_learning_schema

    conn = _new_conn()
    try:
        ensure_learning_schema(conn)
        niches = [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT niche FROM learning_rules"
                " WHERE channel_id = ? AND kind = 'publish_window'"
                " ORDER BY niche ASC",
                (channel_pk,),
            )
        ]
    finally:
        _close(conn)

    store = _store()
    suggestions: list[dict] = []
    active_total = 0
    candidate_total = 0
    for niche_s in niches:
        active = [
            r for r in store.list_active_rules(channel_pk, niche_s)
            if r.get("kind") == "publish_window"
        ]
        candidates = [
            r for r in store.list_candidate_rules(channel_pk, niche_s)
            if r.get("kind") == "publish_window"
        ]
        active_total += len(active)
        candidate_total += len(candidates)
        if not active:
            continue
        best = max(
            active,
            key=lambda r: (
                r.get("evidence_count") or 0,
                r.get("confidence") or 0.0,
                r.get("version") or 0,
            ),
        )
        suggestions.append(
            {
                "niche": niche_s,
                "window": best.get("statement_family"),
                "statement": best.get("statement"),
                "effect_value": best.get("effect_value"),
                "effect_unit": best.get("effect_unit"),
                "evidence_count": best.get("evidence_count"),
                "confidence": best.get("confidence"),
                "is_thin_evidence": bool(best.get("is_thin_evidence")),
                "rule_id": best.get("id"),
                "version": best.get("version"),
            }
        )

    evidence = {
        "active_publish_window_rules": active_total,
        "candidate_publish_window_rules": candidate_total,
        "niches_considered": niches,
        "minimum_required": {
            "active_publish_window_rules": PUBLISH_WINDOW_MIN_ACTIVE_RULES
        },
    }
    if active_total >= PUBLISH_WINDOW_MIN_ACTIVE_RULES:
        return {
            "channel_id": channel_pk,
            "eligible": True,
            "status": "eligible",
            "reason": None,
            "suggestions": suggestions,
            "evidence": evidence,
            "source": "learning_rules",
        }
    return {
        "channel_id": channel_pk,
        "eligible": False,
        "status": "insufficient_data",
        "reason": "not_enough_data",
        "suggestions": [],
        "evidence": evidence,
        "source": "learning_rules",
    }


# ---------------------------------------------------------------------------
# 6. Trigger a learning batch evaluation — engine sibling, bridged to
#    the landed LearningStore via _EngineStoreAdapter
# ---------------------------------------------------------------------------


class _EngineStoreAdapter:
    """Bridge: landed :class:`LearningStore` -> the update engine's
    documented store protocol (:mod:`app.learning_loop.contracts`).

    Field mapping: engine ``effect_estimate`` <-> store
    ``effect_value`` (with ``effect_unit='relative_effect_fraction'``
    — the engine's effects are relative fractions by construction);
    engine dict-shaped ``created_from`` <-> a single JSON string in
    the store's provenance list (see module docstring).  The adapter
    remembers the scope of every rule it touches because the
    protocol's ``activate(rule_id)`` carries no scope while the
    landed store's ``activate_rule`` requires it.

    Contract alignment (Round 5 closure): ``supersede`` forwards
    the engine's ``statement_family`` verbatim, so a direction flip
    is persisted under the NEW family and the old-family rule is
    superseded (R5L-002); and the store's own activation gate
    (n>=3, :class:`app.learning.store.InsufficientEvidenceError`)
    backs the engine's identical gate, so an adapter caller cannot
    activate below the unified threshold either (R5-005).
    """

    def __init__(self, store: LearningStore) -> None:
        self._store = store
        self._scope_by_id: dict[str, tuple[str, str]] = {}

    # -- shaping ------------------------------------------------------
    def _to_contract(self, row: Optional[dict]) -> Optional[dict]:
        if row is None:
            return None
        self._scope_by_id[str(row["id"])] = (
            str(row["channel_id"]),
            str(row["niche"]),
        )
        created_from: dict = {}
        raw = row.get("created_from")
        if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], str):
            try:
                parsed = json.loads(raw[0])
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                created_from = parsed
        return {
            "id": row.get("id"),
            "channel_id": row.get("channel_id"),
            "niche": row.get("niche"),
            "kind": row.get("kind"),
            "statement_family": row.get("statement_family"),
            "statement": row.get("statement"),
            "effect_estimate": row.get("effect_value"),
            "effect_value": row.get("effect_value"),
            "effect_unit": row.get("effect_unit"),
            "evidence_count": row.get("evidence_count"),
            "confidence": row.get("confidence"),
            "status": row.get("status"),
            "version": row.get("version"),
            "created_from": created_from,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def _encode_created_from(fields: dict) -> list[str]:
        payload = fields.get("created_from")
        if isinstance(payload, dict):
            return [json.dumps(payload)]
        if isinstance(payload, (list, tuple)):
            return [str(item) for item in payload]
        return []

    # -- protocol -----------------------------------------------------
    def get_latest(self, channel_id, niche, kind, statement_family):
        rows = self._store.list_rule_history(
            str(channel_id), str(niche), kind=kind,
            statement_family=statement_family,
        )
        live = [r for r in rows if r.get("status") != "superseded"]
        if not live:
            return None
        return self._to_contract(
            max(live, key=lambda r: r.get("version") or 0)
        )

    def create_candidate(self, fields):
        row = self._store.create_candidate(
            str(fields["channel_id"]),
            str(fields["niche"]),
            str(fields["kind"]),
            str(fields["statement"]),
            statement_family=fields.get("statement_family"),
            effect_value=fields.get("effect_estimate"),
            effect_unit="relative_effect_fraction",
            evidence_count=int(fields.get("evidence_count") or 0),
            confidence=fields.get("confidence"),
            created_from=self._encode_created_from(fields),
        )
        return self._to_contract(row)

    def activate(self, rule_id):
        scope = self._scope_by_id.get(str(rule_id))
        if scope is None:
            raise KeyError(f"unknown learning rule {rule_id}")
        row = self._store.activate_rule(
            str(rule_id), channel_id=scope[0], niche=scope[1]
        )
        return self._to_contract(row)

    def supersede(self, old_rule_id, fields):
        # statement_family MUST be forwarded (R5L-002): on a
        # direction flip the engine's fields carry the NEW family
        # (e.g. ``topic_affinity:ocean:negative``) while the old row
        # sits under the opposite one.  Dropping it here made the
        # store inherit the OLD family, so the flipped rule landed
        # mislabelled under the direction it contradicted.
        row = self._store.supersede_rule(
            str(old_rule_id),
            channel_id=str(fields["channel_id"]),
            niche=str(fields["niche"]),
            statement=fields.get("statement"),
            statement_family=fields.get("statement_family"),
            effect_value=fields.get("effect_estimate"),
            effect_unit="relative_effect_fraction",
            evidence_count=(
                int(fields["evidence_count"])
                if fields.get("evidence_count") is not None
                else None
            ),
            confidence=fields.get("confidence"),
            created_from=self._encode_created_from(fields),
        )
        return self._to_contract(row)


class _NicheScopedSource:
    """Wrap an analytics source so a trigger learns ONLY the
    requested niche (the underlying source filters by channel)."""

    def __init__(self, inner: Any, niche_key: str) -> None:
        self._inner = inner
        self._niche_key = niche_key

    def fetch(self, snapshot_point, *, channel_id=None):
        records = self._inner.fetch(snapshot_point, channel_id=channel_id)
        return [r for r in records if str(r.niche) == self._niche_key]


def _canonical_niche_key(niche_s: str) -> str:
    """The analytics source keys evidence by ``niche_profiles.key``;
    accept a profile id too and translate it to its key."""
    conn = _new_conn()
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "niche_profiles" not in tables:
            return niche_s
        row = conn.execute(
            "SELECT key FROM niche_profiles WHERE id = ? OR key = ? LIMIT 1",
            (niche_s, niche_s),
        ).fetchone()
        return str(row[0]) if row is not None else niche_s
    finally:
        _close(conn)


def trigger_learning_batch(
    channel_id: str,
    niche: Any,
    *,
    snapshot_point: Optional[str] = None,
) -> dict:
    """Explicit, user-invoked run of the update engine for one
    (channel, niche) — a trigger, not a scheduler: nothing here
    loops, schedules or retries in the background.

    Runs the engine at D+7 and D+28 (or the single requested point)
    over the real analytics snapshots for this channel + niche and
    returns the batch reports.  Rules are written only through the
    engine into the Learning Store, within this scope.
    """
    channel_pk = resolve_channel_id(channel_id)
    niche_s = _require_niche(niche)
    points = list(SNAPSHOT_POINTS)
    if snapshot_point is not None:
        point = str(snapshot_point).strip()
        if point not in SNAPSHOT_POINTS:
            raise ApiError(
                422,
                "validation_error",
                f"snapshot_point must be one of {list(SNAPSHOT_POINTS)}.",
                details={"field": "snapshot_point", "allowed": list(SNAPSHOT_POINTS)},
            )
        points = [point]

    try:
        from app.learning_loop.adapters import SqliteAnalyticsSource
        from app.learning_loop.engine import UpdateEngine
    except Exception as exc:  # noqa: BLE001 - engine sibling missing
        raise ApiError(
            503,
            "learning_engine_unavailable",
            "The learning update engine is not available in this build.",
            details={"reason": "engine_module_unavailable"},
        ) from exc

    niche_key = _canonical_niche_key(niche_s)
    source = _NicheScopedSource(
        SqliteAnalyticsSource(_conn_factory()), niche_key
    )
    engine = UpdateEngine(_EngineStoreAdapter(_store()), source)

    reports: list[dict] = []
    for point in points:
        report = engine.run_from_source(point, channel_id=channel_pk)
        body = dataclasses.asdict(report)
        body["rules_created"] = report.rules_created
        reports.append(body)
    return {
        "channel_id": channel_pk,
        "niche": niche_s,
        "trigger": "manual",
        "snapshot_points": points,
        "reports": reports,
    }
