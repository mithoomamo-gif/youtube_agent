"""Guardrail enforcement: niche taboo-topic screening (Round 4, D3).

Semantics — read before changing
---------------------------------
The niche profile's taboo list (``NicheProfile.taboo``, persisted as
``niche_profiles.taboo_json``) is a hard guardrail: a topic matching it
must never be produced unattended.  Before this module the list was
only a soft prompt instruction and a late scene-level screen — a
scheduled topic such as "Gambling tricks that always win at the
casino" flowed through schedule tick -> project -> research completely
unimpeded (see ``backend/tests/test_guardrails_automation.py``).

Screening runs at BOTH automation entry points, reusing the one
existing matcher — :func:`app.pipeline.scenes.safety.prompt_matches_taboo`
— never a second matcher:

* **Schedule materialisation** (:meth:`SchedulerTick._materialise` in
  :mod:`app.execution.scheduler`): after topic selection, before the
  project INSERT.  A match aborts the slot via the scheduler's normal
  failure path, so a ``schedule_runs`` row with status ``'failed'``
  (the only refusal status the schema allows) is recorded with
  reason ``guardrail_taboo: ...`` and ``detail_json`` carrying the
  machine-readable ``reason_code`` + matched term.  No project is
  created, no queued topic is consumed, and the schedule advances
  exactly as for any other failed slot — never a silent drop.
* **Research stage entry** (:mod:`app.execution.handlers`, the first
  DAG stage — the seam a manually created / legacy project flows
  through): :func:`check_topic_for_project` mirrors
  :mod:`app.execution.budget_gate`'s parking pattern.  A match parks
  the project at Gate A — ``ensure_approval`` creates the single
  pending Gate A approval, the project goes to
  ``status='awaiting_review'``, the approval's ``reason_code`` is
  stamped ``guardrail_taboo`` with a human-readable ``note`` and the
  figures under ``artefact_versions_json['guardrail']``, and a
  ``guardrail.blocked`` event is emitted (once per park).  The
  handler then raises ``GatePending`` so the runner parks — not
  fails — the research job, and no research artefact is written.

Resume: approving the ``guardrail_taboo`` Gate A approval is the
user's explicit override (the budget_gate pattern): once a
decided/approved approval with that reason code exists, the check is
bypassed for that project.  Rejecting it leaves the project blocked
via the normal gate flow.  A taboo topic therefore never passes
*silently* — it passes only after a recorded human decision.

Profile resolution mirrors the existing pipeline
(``app.integration.stage_bridge.resolve_profile_for_project`` /
``visuals_stage``): the DB row's ``taboo_json`` is authoritative, and
when the row's ``key`` (or the id itself) names a builtin profile, the
builtin profile's taboo list is unioned in — the same terms the scene
safety layer would build its :class:`NicheSafetyProfile` from.
The ``auto:<niche_profile_id>`` sentinel is not a topic and is never
screened here; the research handler screens the *resolved* topic a
stage returns before any artefact is written.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.pipeline.scenes.safety import NicheSafetyProfile, prompt_matches_taboo

__all__ = [
    "GUARDRAIL_TABOO_REASON",
    "parse_taboo_terms",
    "safety_profile_for_niche_row",
    "match_taboo_term",
    "check_topic_for_project",
]

#: approvals.reason_code / schedule_runs reason prefix for a taboo park.
GUARDRAIL_TABOO_REASON = "guardrail_taboo"


# ---------------------------------------------------------------------------
# Taboo list parsing + safety-profile resolution
# ---------------------------------------------------------------------------


def parse_taboo_terms(raw: Any) -> list[str]:
    """Tolerant ``taboo_json`` decode (mirrors stage_bridge._json_list)."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception:
            return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    return []


def _builtin_taboo_terms(key_or_id: Optional[str]) -> list[str]:
    """Taboo terms of the builtin profile named by ``key_or_id``."""
    if not key_or_id:
        return []
    try:
        from app.niches.builtins import BUILTIN_PROFILES

        profile = BUILTIN_PROFILES.get(str(key_or_id).strip().lower())
        if profile is not None:
            return [str(t) for t in (profile.taboo or [])]
    except Exception:
        pass
    return []


def safety_profile_for_niche_row(
    niche_row: Optional[dict], niche_id: Any = None
) -> NicheSafetyProfile:
    """Build the :class:`NicheSafetyProfile` for a ``niche_profiles`` row.

    Same inputs the visuals stage uses: ``niche_id`` + the profile's
    taboo list (DB ``taboo_json`` unioned with the builtin profile's
    taboo when the row resolves to a builtin, as stage_bridge's
    resolution returns the builtin profile for builtin keys).
    """
    row = niche_row if isinstance(niche_row, dict) else {}
    resolved_id = niche_id if niche_id is not None else row.get("id")
    nid = str(resolved_id or "unknown")
    terms: list[str] = []
    for term in parse_taboo_terms(row.get("taboo_json")):
        if term not in terms:
            terms.append(term)
    for source in (row.get("key"), resolved_id):
        for term in _builtin_taboo_terms(
            str(source) if source is not None else None
        ):
            if term not in terms:
                terms.append(term)
    identity = f"{row.get('key') or ''} {nid}".lower()
    return NicheSafetyProfile(
        niche_id=nid,
        is_islamic=("islamic" in identity),
        taboo_terms=terms,
    )


def match_taboo_term(
    topic: Any,
    niche_row: Optional[dict] = None,
    *,
    niche_id: Any = None,
    taboo_terms: Optional[list] = None,
) -> Optional[str]:
    """First taboo term matching ``topic``, or ``None`` (no match).

    The matching itself is delegated verbatim to
    :func:`prompt_matches_taboo` — this helper only resolves the
    safety profile the way the existing pipeline does.
    """
    if not isinstance(topic, str) or not topic.strip():
        return None
    if taboo_terms is not None:
        nid = str(niche_id or "unknown")
        profile = NicheSafetyProfile(
            niche_id=nid,
            is_islamic=("islamic" in nid.lower()),
            taboo_terms=[str(t) for t in taboo_terms],
        )
    else:
        profile = safety_profile_for_niche_row(niche_row, niche_id)
    if not profile.taboo_terms:
        return None
    return prompt_matches_taboo(topic, profile)


# ---------------------------------------------------------------------------
# Project-level check (research entry) — budget_gate parking pattern
# ---------------------------------------------------------------------------


def _load_row(conn_factory: Any, sql: str, params: tuple) -> Optional[dict]:
    conn = conn_factory()
    try:
        try:
            conn.row_factory = __import__("sqlite3").Row
        except Exception:
            pass
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _has_approved_guardrail_override(conn_factory: Any, project_id: str) -> bool:
    """True once the user approved a guardrail_taboo Gate A approval."""
    conn = conn_factory()
    try:
        try:
            conn.execute("SELECT 1 FROM approvals LIMIT 1")
        except Exception:
            return False
        row = conn.execute(
            "SELECT id FROM approvals WHERE project_id = ? AND gate = 'a' "
            "AND reason_code = ? AND decision = 'approved' "
            "AND status = 'decided' LIMIT 1",
            (project_id, GUARDRAIL_TABOO_REASON),
        ).fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _stamp_guardrail_reason(
    conn_factory: Any, approval_id: str, note: str, figures: dict
) -> None:
    """Set reason_code/note (and guardrail figures) on the approval row."""
    conn = conn_factory()
    try:
        try:
            conn.execute(
                "UPDATE approvals SET reason_code = ?, note = ?, "
                "artefact_versions_json = ? WHERE id = ?",
                (
                    GUARDRAIL_TABOO_REASON,
                    note,
                    json.dumps({"guardrail": figures}),
                    approval_id,
                ),
            )
        except Exception:
            conn.execute(
                "UPDATE approvals SET reason_code = ?, note = ? WHERE id = ?",
                (GUARDRAIL_TABOO_REASON, note, approval_id),
            )
        try:
            conn.commit()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _emit_guardrail_blocked(events: Any, payload: dict, project_id: str) -> None:
    """Emit ``guardrail.blocked`` via hub/publish; never raises."""
    if events is None:
        return
    try:
        publish = getattr(events, "publish", None)
        if callable(publish):
            publish(
                "guardrail.blocked", payload, project_id=project_id, job_id=None
            )
            return
    except Exception:
        pass
    try:
        if callable(events):
            events("guardrail.blocked", payload)
    except Exception:
        pass


def check_topic_for_project(
    conn_factory: Any,
    project_id: Any,
    events: Any = None,
    *,
    project: Optional[dict] = None,
    topic_override: Optional[str] = None,
) -> Optional[dict]:
    """Park ``project_id`` at Gate A if its topic matches a taboo term.

    Returns ``None`` when the topic may proceed (no taboo configured,
    no match, the ``auto:`` sentinel — the resolved topic is screened
    separately via ``topic_override`` — or an approved guardrail
    override exists).  Otherwise parks the project exactly like
    :func:`app.execution.budget_gate.check_budget_for_project` parks a
    budget breach and returns ``{paused: True, reason:
    'guardrail_taboo', approval_id, gate: 'a', matched_taboo_term,
    topic, niche_profile_id, ...}``.
    """
    project_id_str = str(project_id)
    project_row = (
        project
        if isinstance(project, dict)
        else _load_row(
            conn_factory,
            "SELECT * FROM projects WHERE id = ?",
            (project_id_str,),
        )
    )
    if project_row is None:
        return None
    project_id_str = str(project_row.get("id") or project_id_str)

    topic = topic_override or project_row.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return None
    if topic_override is None and topic.strip().startswith("auto:"):
        # Sentinel, not a topic: research ideates the real topic
        # later; the handler screens that resolved topic (via
        # topic_override) before writing any research artefact.
        return None

    niche_id = project_row.get("niche_profile_id")
    niche_row = None
    if niche_id is not None:
        niche_row = _load_row(
            conn_factory,
            "SELECT * FROM niche_profiles WHERE id = ?",
            (str(niche_id),),
        )
    matched = match_taboo_term(topic, niche_row, niche_id=niche_id)
    if matched is None:
        return None

    if _has_approved_guardrail_override(conn_factory, project_id_str):
        # Explicit human override (budget_gate pattern): a decided,
        # approved guardrail approval lets this one project proceed.
        return None

    figures = {
        "matched_taboo_term": matched,
        "topic": topic,
        "niche_profile_id": str(niche_id) if niche_id is not None else None,
        "channel_id": (
            str(project_row["channel_id"])
            if project_row.get("channel_id") is not None
            else None
        ),
    }

    # Park at Gate A via the shared bridge (idempotent: one pending
    # approval per (project, gate); project -> awaiting_review).
    from app.execution import gates_bridge

    already_parked = False
    try:
        existing = gates_bridge.find_pending_approval(
            conn_factory, project_id_str, "a"
        )
        already_parked = bool(
            existing and existing.get("reason_code") == GUARDRAIL_TABOO_REASON
        )
    except Exception:
        already_parked = False

    job_stub = {
        "project_id": project_id_str,
        "payload": {"project_id": project_id_str, "stage": "gate_a"},
    }
    approval_id = gates_bridge.ensure_approval(
        conn_factory, project_row, "a", job_stub, events=None
    )
    note = (
        f"Guardrail: topic {topic!r} matches niche taboo blocked term "
        f"{matched!r} — production parked before research. Approve to "
        f"override the guardrail for this project, or reject to keep "
        f"it blocked."
    )
    _stamp_guardrail_reason(conn_factory, str(approval_id), note, figures)

    result = {
        "paused": True,
        "reason": GUARDRAIL_TABOO_REASON,
        "reason_code": GUARDRAIL_TABOO_REASON,
        "gate": "a",
        "approval_id": str(approval_id),
        "project_id": project_id_str,
        **figures,
    }
    if not already_parked:
        _emit_guardrail_blocked(events, dict(result), project_id_str)
    return result
