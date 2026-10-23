"""Publish finaliser for the scheduled (DAG) path — Round 4, Worker 6.

The execution DAG (``app.execution.dag``) is a 10-job chain ENDING at
``gate.b``.  Until now nothing after ``gate.b`` moved a scheduled
project into a publish state: the monolith path
(``app.integration.produce`` + ``app.integration.publish_stage``) knew
how to publish (fake provider only), but a project produced by the
*scheduler -> DAG -> runner* path simply stopped at the gate.  This
module is the terminal step for that path:

``gate.b`` job completes with decision ``approved`` / ``auto_approved``
    -> :func:`finalise_after_gate_b`
    -> publish_stage fake-provider flow for the project's pack
    -> exactly one ``publish_records`` row
    -> project moved to its honest terminal publish state
    -> ``project.publish_simulated`` live event + persisted
       ``event_log`` row

It is wired into :class:`app.execution.service.ExecutionService` by
wrapping the registry's ``gate.b`` handler: finalisation hooks *gate
completion* (the handler's return), never approval creation — so in
``review_first`` mode nothing publishes until the human approval has
landed and the approvals flow (``gates_bridge.apply_decision``) has
resumed the gate job to completion, while in ``auto`` mode the
finalisation happens unattended in the same run.

Provenance honesty (hard rules)
--------------------------------
* The ONLY provider ever used is the fake one from
  ``app.integration.publish_stage`` (``provider='fake'``,
  ``simulated=True``).  Real YouTube upload is out of scope — it awaits
  interactive channel sign-in.
* ``projects.youtube_video_id`` and ``projects.published_at`` are left
  NULL and ``publish_records.video_url`` is left NULL: no real video
  exists, so no real id / URL / publish time is fabricated.
* The fake provider mints ids like ``fake-video-1`` (per-instance
  sequence — not globally unique and easy to mistake for a real id),
  so the value stored in ``publish_records.youtube_video_id`` is
  namespaced: ``simulated:<provider_video_id>:<project_id>`` —
  unmistakably simulated and unique per project.  The raw provider id
  is kept in the provenance payload (``gate_status_json.publish`` and
  the event) for traceability.

Terminal-state semantics (decision, with code evidence)
--------------------------------------------------------
A simulated publish lands the project at ``status='ready'`` — NOT
``'published'``:

* ``app.integration.publish_stage.finalize_publish`` reports the fake
  run as ``publish_state='fake_provider_upload_simulated'`` and never
  as a real publication; ``app.integration.produce`` mirrors that.
* The publishing state machine (``app.pipeline.publishing``) is
  explicit: "Nothing is announced as published until LIVE_CONFIRMED"
  — a fake-provider simulation is not a live confirmation.
* ``projects.status='published'`` is the UI's published counter
  (``src/api/queries/mission.ts`` ``PUBLISHED_STATUS``, Costs screen),
  so stamping it for a simulation would misreport a video that does
  not exist on YouTube.

``'ready'`` therefore means: *production finished, Gate B approved,
simulated publish recorded; ready for a real upload once a channel is
connected*.  The full simulated provenance lives in
``projects.gate_status_json['publish']`` (``state`` =
``fake_provider_upload_simulated``, ``provider='fake'``,
``simulated=True``, publish record id, provider video id).
``projects.current_phase`` is deliberately NOT touched — it is owned
by the DAG (final phase ``packaging``).

Pack tiers
----------
* **Pack on disk** — a ``master_video`` artefact (or stage job result)
  points at a real master file: the full
  :func:`app.integration.publish_stage.finalize_publish` flow runs
  (copyright/policy/QC Gate B tokens bound to the frozen manifest,
  then the fake-provider workflow).  If THAT Gate B fails, no publish
  record is written and the project is marked ``blocked`` with the
  problems in ``error_summary`` — visible, never silent.
* **No pack on disk** — the DAG's stub stages produced only
  ``stub://`` artefacts, so there is no master to freeze a manifest
  against.  The publish_stage :class:`FakeProvider` is then driven
  directly (one resumable session, zero-byte completion) and the
  record is marked ``pack_flow='fake_provider_direct_no_pack'`` in
  its provenance.  This is still only ever a simulation.

Exactly-once
------------
1. ``publish_records.project_id`` is UNIQUE in the schema, as are
   ``upload_idempotency_key`` and ``youtube_video_id``.
2. The finaliser checks for an existing record first and returns it
   (``already_finalised=True``) — DAG replays, service restarts and
   duplicate gate completions all converge here.
3. The record insert + project update run inside one
   ``BEGIN IMMEDIATE`` transaction that re-checks for an existing
   record; a racing insert loses on the UNIQUE constraints and the
   loser returns the winner's record instead of a second row.
4. The upload idempotency key is deterministic: the frozen manifest's
   own ``idempotency_key`` (pack tier) or
   ``simulated-publish:<project_id>`` (no-pack tier).

Gate B alignment (Round 6 closure, W7)
---------------------------------------
This finaliser does NOT apply a second, stricter Gate B verdict.
It re-runs the package stage's own evaluator
(``publish_stage.finalize_publish`` — strict ``verify_gate_b`` plus
the Round-4 conditional policy in ``evaluate_gate_b``) with the
project's publish mode and with this Gate B decision as the
recorded approval, and it reports the result with the shared
``gate_b_verdict`` label / ``gate_b_reason_codes`` that the package
stage froze into the manifest.  Provider canonicalisation
(``local_plate`` -> ``generated``) lives in that shared finalize
code, so the asset registry is judged identically on both paths:
the frozen-produce smoke divergence (package ``gate_b_passed=True``
for local plates vs finaliser ``G2_PROVENANCE_FAILED`` over the
same plates) cannot recur without both sides changing together.

Blocked / failed gates
-----------------------
When Gate B does not approve, :func:`record_non_publication` merges
``gate_status_json['publish'] = {state: 'not_published', reason}`` and
fills ``error_summary`` (when empty) — the non-publication is visible
in the project record.  The project's status itself is left exactly
where the gate left it (``blocked`` / ``failed`` / …), except for the
pack-tier Gate B failure above, which actively marks ``blocked``
because the DAG gate approved a pack whose own publish Gate B failed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.logging_setup import get_logger

logger = get_logger("app.execution.publish_finaliser")

__all__ = [
    "finalise_after_gate_b",
    "record_non_publication",
    "PROVIDER",
    "SIMULATED",
    "TERMINAL_STATUS",
    "EVENT_PUBLISH_SIMULATED",
    "EVENT_PUBLISH_SKIPPED",
    "EVENT_PUBLISH_BLOCKED",
]

#: The only provider this finaliser will ever use.
PROVIDER = "fake"
#: Every record this module writes is a simulation.
SIMULATED = True
#: Honest terminal project status for a simulated publish (see docstring).
TERMINAL_STATUS = "ready"

EVENT_PUBLISH_SIMULATED = "project.publish_simulated"
EVENT_PUBLISH_SKIPPED = "project.publish_skipped"
EVENT_PUBLISH_BLOCKED = "project.publish_blocked"

_APPROVED_DECISIONS = ("approved", "auto_approved")
_VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".m4v")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# DB helpers (fresh short connections, like the rest of the execution layer)
# ---------------------------------------------------------------------------


def _connect(conn_factory) -> sqlite3.Connection:
    conn = conn_factory()
    try:
        conn.row_factory = sqlite3.Row
    except Exception:
        pass
    return conn


def _commit_quietly(conn) -> None:
    try:
        conn.commit()
    except Exception:
        pass


def _load_project(conn, project_id: str) -> Optional[dict]:
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    except Exception:
        return None
    return dict(row) if row is not None else None


def _existing_record(conn, project_id: str) -> Optional[dict]:
    try:
        row = conn.execute(
            "SELECT * FROM publish_records WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    except Exception:
        return None
    return dict(row) if row is not None else None


def _parse_json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
    return default


def _gate_status(project: dict) -> dict:
    parsed = _parse_json(project.get("gate_status_json"), default={})
    return dict(parsed) if isinstance(parsed, dict) else {}


def _merge_project_publish(
    conn,
    project_id: str,
    publish_info: dict,
    *,
    status: Optional[str] = None,
    error_summary: Optional[str] = None,
) -> None:
    """Merge ``publish`` into ``gate_status_json`` (+ optional status /
    error_summary) on the project row.  Caller owns the transaction."""
    row = conn.execute(
        "SELECT gate_status_json, error_summary FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return
    gate_status = _parse_json(row[0], default={})
    if not isinstance(gate_status, dict):
        gate_status = {}
    gate_status["publish"] = publish_info
    sets = ["gate_status_json = ?", "updated_at = ?"]
    params: list[Any] = [json.dumps(gate_status), _now_ms()]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if error_summary is not None and not row[1]:
        sets.append("error_summary = ?")
        params.append(error_summary)
    params.append(project_id)
    conn.execute(
        f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params
    )


# ---------------------------------------------------------------------------
# Events: live emit (hub / emitter) + persisted event_log row
# ---------------------------------------------------------------------------


def _walk_event_objects(events: Any) -> list[Any]:
    """Candidate objects behind an events facade, outermost first."""
    seen: list[Any] = []
    queue = [events]
    while queue:
        obj = queue.pop(0)
        if obj is None or any(obj is s for s in seen):
            continue
        seen.append(obj)
        for attr in ("_hub", "hub", "_emitter", "emitter"):
            try:
                queue.append(getattr(obj, attr, None))
            except Exception:
                pass
    return seen


def _hub_persists(events: Any) -> Optional[str]:
    """Boot id of a journal-persisting hub behind ``events``, if any.

    The real :class:`app.ws_hub.WSHub` persists every published frame
    to ``event_log`` when it has a conn_factory; when one is in the
    chain the finaliser must not write a second journal row.
    """
    for obj in _walk_event_objects(events):
        boot_id = getattr(obj, "boot_id", None)
        if boot_id and getattr(obj, "_conn_factory", None) is not None:
            return str(boot_id)
    return None


def _boot_id_for(events: Any) -> str:
    for obj in _walk_event_objects(events):
        boot_id = getattr(obj, "boot_id", None)
        if boot_id:
            return str(boot_id)
    return "publish-finaliser"


def _emit_event(
    conn_factory,
    events: Any,
    event_type: str,
    payload: dict,
    *,
    project_id: str,
    job_id: Optional[str] = None,
) -> None:
    """Emit a live event (never raises) and ensure an event_log row.

    Live delivery goes through ``events.publish`` when available.  The
    persisted journal row is written by the hub itself when a
    persisting WSHub is behind ``events``; otherwise this function
    inserts the row directly (same envelope shape the hub stores).
    """
    body = dict(payload)
    body.setdefault("ts", _now_ms())

    delivered = False
    if events is not None:
        for method_name in ("publish", "emit"):
            method = getattr(events, method_name, None)
            if not callable(method):
                continue
            try:
                method(event_type, body, project_id=project_id, job_id=job_id)
                delivered = True
                break
            except TypeError:
                try:
                    method(event_type, body)
                    delivered = True
                    break
                except Exception:
                    continue
            except Exception:
                continue

    if _hub_persists(events) is not None and delivered:
        return  # the hub already journalled this frame

    # Direct journal insert (best-effort; the live emit above is the
    # primary channel and a journal failure must not fail publishing).
    try:
        conn = _connect(conn_factory)
        try:
            boot_id = _boot_id_for(events)
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM event_log WHERE boot_id = ?",
                (boot_id,),
            ).fetchone()
            seq = int(row[0] or 0) + 1 if row is not None else 1
            envelope = {
                "v": 1,
                "type": event_type,
                "event_id": uuid.uuid4().hex,
                "seq": seq,
                "boot_id": boot_id,
                "ts": _now_ms(),
                "payload": body,
                "project_id": project_id,
            }
            if job_id is not None:
                envelope["job_id"] = job_id
            conn.execute(
                "INSERT INTO event_log (id, seq, boot_id, type,"
                " payload_json, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    envelope["event_id"],
                    seq,
                    boot_id,
                    event_type,
                    json.dumps(envelope),
                    _now_ms(),
                ),
            )
            _commit_quietly(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — journal is best-effort
        logger.warning("event_log persist failed for %s: %s", event_type, exc)


# ---------------------------------------------------------------------------
# Pack discovery (artefacts + stage job results recorded by the DAG)
# ---------------------------------------------------------------------------


def _result_dicts(conn, project_id: str) -> list[tuple[str, dict]]:
    """(kind, result-dict) for the project's stage jobs, newest first,
    each followed by its nested ``artefact`` dict when present."""
    out: list[tuple[str, dict]] = []
    try:
        rows = conn.execute(
            "SELECT kind, result_json FROM jobs WHERE project_id = ?"
            " AND result_json IS NOT NULL ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
    except Exception:
        return out
    seen_kinds: set[str] = set()
    for row in rows:
        kind = str(row[0])
        if kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        parsed = _parse_json(row[1], default=None)
        if not isinstance(parsed, dict):
            continue
        out.append((kind, parsed))
        artefact = parsed.get("artefact")
        if isinstance(artefact, dict):
            out.append((kind, artefact))
    # Agent runs carry the same result JSON in output_ref_json.
    try:
        rows = conn.execute(
            "SELECT agent_key, output_ref_json FROM agent_runs"
            " WHERE project_id = ? AND output_ref_json IS NOT NULL"
            " ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    except Exception:
        rows = []
    for row in rows:
        parsed = _parse_json(row[1], default=None)
        if isinstance(parsed, dict):
            out.append((str(row[0]), parsed))
    return out


def _artefacts_by_type(conn, project_id: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "SELECT * FROM project_artefacts WHERE project_id = ?"
            " ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    except Exception:
        return out
    for row in rows:
        item = dict(row)
        out.setdefault(str(item.get("artefact_type")), item)
    return out


def _is_real_video_file(value: Any) -> Optional[Path]:
    if not isinstance(value, str) or not value or "://" in value:
        return None
    if not value.lower().endswith(_VIDEO_SUFFIXES):
        return None
    try:
        path = Path(value)
        return path if path.is_file() else None
    except OSError:
        return None


def _first_value(dicts: list[dict], *keys: str) -> Any:
    for data in dicts:
        for key in keys:
            value = data.get(key)
            if value is not None and value != "":
                return value
    return None


def _discover_pack(conn, project_id: str) -> dict:
    """Everything the publish_stage flow needs, from DAG records."""
    results = _result_dicts(conn, project_id)
    dicts = [d for _, d in results]
    artefacts = _artefacts_by_type(conn, project_id)

    master: Optional[Path] = None
    master_artefact = artefacts.get("master_video") or {}
    master = _is_real_video_file(master_artefact.get("file_path"))
    if master is None:
        for data in dicts:
            for key in ("final_mp4", "final_path", "master_path", "path",
                        "file_path"):
                master = _is_real_video_file(data.get(key))
                if master is not None:
                    break
            if master is not None:
                break

    duration_raw = _first_value(
        dicts, "duration_s", "duration_seconds", "duration")
    try:
        duration_s = float(duration_raw) if duration_raw is not None else 0.0
    except (TypeError, ValueError):
        duration_s = 0.0

    thumbnails: list[str] = []
    raw_thumbs = _first_value(dicts, "thumbnails")
    if isinstance(raw_thumbs, (list, tuple)):
        thumbnails = [str(t) for t in raw_thumbs if t]
    thumb_artefact = artefacts.get("thumbnail") or {}
    if thumb_artefact.get("file_path") and "://" not in str(
            thumb_artefact["file_path"]):
        thumbnails.insert(0, str(thumb_artefact["file_path"]))

    licence_segments = _first_value(
        dicts, "licence_segments", "licence_rows", "license_rows")
    if not isinstance(licence_segments, list):
        licence_segments = []

    qc_summary = _first_value(dicts, "qc_summary")
    if not isinstance(qc_summary, dict):
        qc_summary = None
    qc_verdict = _first_value(dicts, "verdict")
    if not qc_verdict and qc_summary:
        # The assemble/QC artefacts carry the verdict inside
        # qc_summary (with the per-check reason codes the conditional
        # policy needs), not as a top-level job-result key.
        qc_verdict = qc_summary.get("verdict")
    qc_checks = {}
    if qc_summary and isinstance(qc_summary.get("checks"), dict):
        qc_checks = qc_summary["checks"]
    qc_passed_raw = _first_value(dicts, "qc_passed", "qcPassed")
    qc_passed = (
        bool(qc_passed_raw) if isinstance(qc_passed_raw, bool) else None
    )

    pack_dir: Optional[Path] = master.parent if master is not None else None
    if pack_dir is None:
        pack_artefact = artefacts.get("metadata_pack") or {}
        candidate = pack_artefact.get("file_path")
        if isinstance(candidate, str) and "://" not in candidate:
            path = Path(candidate)
            pack_dir = path if path.is_dir() else path.parent

    return {
        "pack_present": master is not None,
        "master_path": master,
        "duration_s": duration_s,
        "thumbnails": thumbnails,
        "captions_path": _first_value(
            dicts, "captions_path", "caption_path", "captions_srt"),
        "licence_segments": licence_segments,
        "qc_verdict": str(qc_verdict) if qc_verdict else None,
        "qc_passed": qc_passed,
        "qc_summary": qc_summary,
        "qc_checks": qc_checks,
        "title": _first_value(dicts, "title", "video_title"),
        "description": _first_value(dicts, "description", "video_description"),
        "tags": _first_value(dicts, "tags", "keywords"),
        "script_text": _first_value(dicts, "script_text", "script"),
        "thumbnail_artefact_id": (artefacts.get("thumbnail") or {}).get("id"),
        "pack_dir": pack_dir,
    }


# ---------------------------------------------------------------------------
# Compliance Gate (Round 6): the finaliser's own DB is the source of truth
# ---------------------------------------------------------------------------


def _compliance_for(conn_factory) -> tuple[str, dict]:
    """(overall, visibility decision) from the finaliser's database.

    A publish through this path requests public visibility by
    intent; pre-PASS the decision forces private and refuses the
    public publish (typed reason ``compliance_not_passed``).  Any
    failure to read a stored state resolves to ``not_started``.
    """
    try:
        from app.compliance.gate import evaluate_visibility
        from app.compliance.service import overall_from_conn

        conn = _connect(conn_factory)
        try:
            overall = overall_from_conn(conn)
        finally:
            conn.close()
        return overall, dict(evaluate_visibility("public", overall))
    except Exception:
        try:
            from app.compliance.gate import evaluate_visibility

            return "not_started", dict(
                evaluate_visibility("public", "not_started")
            )
        except Exception:
            return "not_started", {
                "overall": "not_started",
                "passed": False,
                "requested_visibility": "public",
                "effective_visibility": "private",
                "forced_private": True,
                "public_publish_refused": True,
                "reason_code": "compliance_not_passed",
            }


# ---------------------------------------------------------------------------
# Publish flows (fake provider only)
# ---------------------------------------------------------------------------


def _run_pack_flow(
    project: dict, pack: dict, gate_a_passed: bool, compliance_state: Any = None,
    *, mode: str = "review_first", gate_b_approval: Any = None,
) -> Any:
    """The full publish_stage flow for a pack that exists on disk.

    Round 6 closure (W7): this re-runs the SAME
    :func:`app.integration.publish_stage.finalize_publish` evaluator
    the package stage ran — there is no second, stricter evaluator
    here.  Two context pieces must therefore match / complete the
    package-stage evaluation, and both are now passed explicitly:

    * the project's publish ``mode`` (a conditional in ``auto`` mode
      blocks here exactly as the package report labels it), and
    * ``gate_b_approval`` — the Gate B decision that triggered this
      finalisation IS the recorded human approval the Round-4
      conditional rule requires in ``review_first`` mode.  Omitting
      it (the old behaviour) made a Gate-B-approved conditional
      re-block with ``CONDITIONAL_APPROVAL_REQUIRED`` at publish
      time, contradicting the approval itself.

    The licence segments are passed through RAW (provider
    ``local_plate`` included): provider canonicalisation lives in
    the shared finalize code, so this path and the package stage
    judge the identical registry identically.
    """
    from app.integration.publish_stage import finalize_publish

    master = pack["master_path"]
    assemble = {
        "final_mp4": str(master),
        "duration_s": pack["duration_s"],
        "thumbnails": pack["thumbnails"],
        "captions_path": pack["captions_path"],
        "licence_segments": pack["licence_segments"],
        "qc_passed": bool(pack["qc_passed"]),
        "qc_summary": pack["qc_summary"],
        "pack_dir": str(pack["pack_dir"] or master.parent),
        "project_id": project["id"],
    }
    qc_info: dict = {}
    if pack["qc_verdict"]:
        # Carry the per-check reason codes too: dropping them (the
        # old ``checks: {}``) stripped the QC flags the conditional
        # classification reads, so a minor-only WARN could not be
        # recognised as conditional on this path at all.
        qc_info = {
            "verdict": pack["qc_verdict"],
            "checks": pack.get("qc_checks") or {},
        }
    bundle = {
        "title": pack["title"] or project.get("topic") or "Untitled Video",
        "description": pack["description"] or "",
        "tags": pack["tags"] if isinstance(pack["tags"], list) else [],
        "script_text": pack["script_text"] or "",
    }
    ctx = {
        "pack_dir": str(pack["pack_dir"] or master.parent),
        "project_id": project["id"],
        "channel_id": project.get("channel_id") or "channel",
    }
    return finalize_publish(
        assemble, qc_info, bundle, None, ctx, gate_a_passed,
        compliance_state=compliance_state,
        mode=mode,
        gate_b_approval=gate_b_approval,
    )


def _run_direct_fake_flow(project: dict, compliance_decision: Optional[dict] = None) -> dict:
    """publish_stage's FakeProvider driven directly (no pack on disk).

    One resumable session with a zero-byte payload: the fake provider
    completes it and mints its (simulated) video id.  No workflow /
    manifest is fabricated — the provenance records exactly this.
    The insert metadata is private by construction (the provider
    default); pre-PASS that private is FORCED by the Compliance Gate
    and the outcome says so.
    """
    from app.integration.publish_stage import FakeProvider
    from app.pipeline.publishing.provider import VideoMetadata

    fake = FakeProvider()
    metadata = VideoMetadata(
        title=str(project.get("topic") or "Untitled Video")[:100],
        description="",
        tags=[],
        category_id="22",
        contains_synthetic_media=True,
    )
    forced_private = bool(
        compliance_decision and not compliance_decision.get("passed")
    )
    session_uri = fake.create_resumable_session(metadata, 0)
    result = fake.upload_chunk(session_uri, b"", 0)
    if not result.complete or not result.video_id:
        raise RuntimeError("fake provider did not complete the simulated upload")
    return {
        "provider_video_id": result.video_id,
        "publish_state": "fake_provider_upload_simulated",
        "idempotency_key": f"simulated-publish:{project['id']}",
        "title": metadata.title,
        "description": "",
        "tags": [],
        "manifest_path": None,
        "privacy_status": "private",
        "forced_private_by_compliance_gate": forced_private,
        "compliance_gate": compliance_decision,
    }


# ---------------------------------------------------------------------------
# Commit: publish_records insert + project terminal state (exactly once)
# ---------------------------------------------------------------------------


def _already_finalised(record: dict) -> dict:
    return {
        "finalised": True,
        "already_finalised": True,
        "published_simulated": True,
        "provider": PROVIDER,
        "simulated": SIMULATED,
        "publish_record_id": record.get("id"),
        "simulated_video_id": record.get("youtube_video_id"),
        "terminal_status": TERMINAL_STATUS,
    }


def _commit_publication(
    conn_factory,
    project: dict,
    outcome: dict,
    *,
    pack_flow: str,
    compliance_gate: Optional[dict] = None,
) -> dict:
    """Insert the publish record + move the project, exactly once."""
    project_id = str(project["id"])
    provider_video_id = outcome.get("provider_video_id")
    simulated_video_id = (
        f"simulated:{provider_video_id}:{project_id}"
        if provider_video_id
        else f"simulated:{project_id}"
    )
    record_id = str(uuid.uuid4())
    now = _now_ms()
    description = outcome.get("description") or ""
    # Compliance Gate disclosure (Round 6): the publish record is
    # ALWAYS private on this simulated path; pre-PASS that private
    # is forced by the gate and the provenance must SAY so.
    compliance = compliance_gate or outcome.get("compliance_gate") or {}
    forced_private = bool(
        outcome.get("forced_private_by_compliance_gate")
        or (compliance and not compliance.get("passed"))
    )
    provenance = {
        "state": outcome.get("publish_state") or "fake_provider_upload_simulated",
        "provider": PROVIDER,
        "simulated": SIMULATED,
        "publish_record_id": record_id,
        "provider_video_id": provider_video_id,
        "simulated_video_id": simulated_video_id,
        "pack_flow": pack_flow,
        "manifest_path": outcome.get("manifest_path"),
        "finalised_at": now,
        "privacy_status": "private",
        "requested_visibility": compliance.get("requested_visibility", "public"),
        "effective_visibility": "private",
        "forced_private_by_compliance_gate": forced_private,
        "compliance_gate": compliance or None,
        # Shared Gate B verdict labelling (W7), when the pack flow
        # produced one: a conditional release is recorded AS
        # "conditional_pass" in the publish provenance, never as an
        # unlabelled pass.
        "gate_b_verdict": outcome.get("gate_b_verdict"),
        "gate_b_outcome": outcome.get("gate_b_outcome"),
        "gate_b_reason_codes": outcome.get("gate_b_reason_codes") or [],
    }

    conn = _connect(conn_factory)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except Exception:
            pass  # connection may already be in a transaction
        existing = _existing_record(conn, project_id)
        if existing is not None:
            _commit_quietly(conn)
            return _already_finalised(existing)
        try:
            conn.execute(
                "INSERT INTO publish_records (id, project_id,"
                " youtube_video_id, video_url, title, description_hash,"
                " tags_json, thumbnail_artefact_id, privacy_status,"
                " scheduled_for, disclosure_altered_content,"
                " upload_idempotency_key, published_at, created_at)"
                " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 'private', NULL, 1,"
                " ?, NULL, ?)",
                (
                    record_id,
                    project_id,
                    simulated_video_id,
                    (outcome.get("title") or project.get("topic") or "")[:500]
                    or None,
                    hashlib.sha256(description.encode("utf-8")).hexdigest()
                    if description else None,
                    json.dumps(outcome.get("tags") or [])
                    if outcome.get("tags") else None,
                    outcome.get("thumbnail_artefact_id"),
                    str(outcome["idempotency_key"]),
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            # Lost a race (or a replay hit a UNIQUE constraint): the
            # winner's row is the single record for this project.
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            existing = _existing_record(conn, project_id)
            if existing is not None:
                return _already_finalised(existing)
            raise
        _merge_project_publish(conn, project_id, provenance,
                               status=TERMINAL_STATUS)
        _commit_quietly(conn)
    finally:
        conn.close()

    return {
        "finalised": True,
        "already_finalised": False,
        "published_simulated": True,
        "provider": PROVIDER,
        "simulated": SIMULATED,
        "publish_record_id": record_id,
        "provider_video_id": provider_video_id,
        "simulated_video_id": simulated_video_id,
        "publish_state": provenance["state"],
        "pack_flow": pack_flow,
        "terminal_status": TERMINAL_STATUS,
        "privacy_status": "private",
        "forced_private_by_compliance_gate": forced_private,
        "compliance_gate": compliance or None,
    }


def _mark_blocked(
    conn_factory,
    project: dict,
    *,
    reason: str,
    detail: Optional[str] = None,
    events: Any = None,
    gate_job_id: Optional[str] = None,
    mark_status_blocked: bool = False,
    publish_extra: Optional[dict] = None,
) -> dict:
    """Make a non-publication visible in the project record (+ event)."""
    project_id = str(project["id"])
    publish_info = {
        "state": "not_published",
        "provider": PROVIDER,
        "simulated": False,
        "reason": reason,
        "detail": detail,
        "recorded_at": _now_ms(),
    }
    if publish_extra:
        # Shared Gate B verdict labelling (W7): the refusal carries
        # the same verdict label / typed reason codes the package
        # stage reported for this evaluation.
        publish_info.update(publish_extra)
    conn = _connect(conn_factory)
    try:
        _merge_project_publish(
            conn,
            project_id,
            publish_info,
            status="blocked" if mark_status_blocked else None,
            error_summary=f"Not published: {reason}"
            + (f" — {detail}" if detail else ""),
        )
        _commit_quietly(conn)
    finally:
        conn.close()
    _emit_event(
        conn_factory,
        events,
        EVENT_PUBLISH_BLOCKED if mark_status_blocked else EVENT_PUBLISH_SKIPPED,
        {
            "project_id": project_id,
            "provider": PROVIDER,
            "simulated": False,
            "reason": reason,
            "detail": detail,
        },
        project_id=project_id,
        job_id=gate_job_id,
    )
    result = {
        "finalised": False,
        "published_simulated": False,
        "provider": PROVIDER,
        "simulated": False,
        "reason": reason,
        "detail": detail,
    }
    if publish_extra:
        result.update(publish_extra)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_non_publication(
    conn_factory,
    project_id: str,
    *,
    reason: str,
    events: Any = None,
    gate_job_id: Optional[str] = None,
) -> dict:
    """Record, visibly, that a project was NOT published (blocked /
    failed gate).  Never touches the project's status and never writes
    a publish record.  A project that already has a publish record is
    left untouched (its provenance is the truth)."""
    conn = _connect(conn_factory)
    try:
        project = _load_project(conn, str(project_id))
        if project is None:
            return {"finalised": False, "reason": "project_not_found"}
        existing = _existing_record(conn, str(project_id))
    finally:
        conn.close()
    if existing is not None:
        return _already_finalised(existing)
    return _mark_blocked(
        conn_factory,
        project,
        reason=reason,
        events=events,
        gate_job_id=gate_job_id,
    )


def finalise_after_gate_b(
    conn_factory,
    project_id: str,
    *,
    decision: Optional[str] = None,
    events: Any = None,
    gate_job_id: Optional[str] = None,
) -> dict:
    """Finalise one project after its ``gate.b`` job completed.

    ``decision`` is the gate job's decision when the caller has it
    (the service wrapper passes the handler result); otherwise it is
    read back from ``projects.gate_status_json['gate_b']``.  Anything
    other than ``approved`` / ``auto_approved`` records a visible
    non-publication and publishes nothing.
    """
    project_id = str(project_id)
    conn = _connect(conn_factory)
    try:
        project = _load_project(conn, project_id)
        if project is None:
            return {"finalised": False, "reason": "project_not_found"}
        existing = _existing_record(conn, project_id)
        if existing is not None:
            return _already_finalised(existing)
        gate_status = _gate_status(project)
        pack = _discover_pack(conn, project_id)
    finally:
        conn.close()

    resolved = (decision or gate_status.get("gate_b") or "").strip().lower()
    if resolved not in _APPROVED_DECISIONS:
        return _mark_blocked(
            conn_factory,
            project,
            reason=f"gate_b_decision_{resolved or 'missing'}",
            detail="Gate B did not approve this project; nothing was published.",
            events=events,
            gate_job_id=gate_job_id,
        )

    gate_a_passed = str(gate_status.get("gate_a") or "").strip().lower() in (
        _APPROVED_DECISIONS
    )

    compliance_overall, compliance_decision = _compliance_for(conn_factory)

    # Evaluation context for the shared Gate B evaluator (W7): the
    # project's own publish mode, and the Gate B decision itself as
    # the recorded approval a Round-4 conditional requires in
    # review_first mode (in auto mode the evaluator blocks a
    # conditional regardless of this record — semantics preserved).
    publish_mode = str(project.get("publish_mode") or "review_first").strip().lower()
    if publish_mode not in ("review_first", "auto"):
        publish_mode = "review_first"
    gate_b_approval = {
        "approved": True,
        "actor": "gate_b",
        "approval_id": f"gate_b:{resolved}",
    }

    if pack["pack_present"]:
        pack_flow = "publish_stage"
        try:
            result = _run_pack_flow(
                project, pack, gate_a_passed, compliance_state=compliance_overall,
                mode=publish_mode, gate_b_approval=gate_b_approval,
            )
        except Exception as exc:  # noqa: BLE001 — honest blocked, not a crash
            return _mark_blocked(
                conn_factory,
                project,
                reason="publish_stage_error",
                detail=f"{type(exc).__name__}: {exc}",
                events=events,
                gate_job_id=gate_job_id,
                mark_status_blocked=True,
            )
        if not result.gate_b_passed or (
            result.publish_state != "fake_provider_upload_simulated"
            or not result.video_id
        ):
            # Same typed reasons the package stage reported (W7):
            # problem codes PLUS the shared reason-code list, so the
            # refusal names e.g. G2_PROVENANCE_FAILED — not only the
            # generic VERDICT_NOT_PASS the strict check produces.
            codes: list[str] = []
            for p in (result.gate_b_problems or []):
                code = str(p.get("code") or p)
                if code and code not in codes:
                    codes.append(code)
            for code in (getattr(result, "gate_b_reason_codes", None) or []):
                if str(code) not in codes:
                    codes.append(str(code))
            problems = "; ".join(codes) or result.publish_state
            gate_b_verdict = getattr(result, "gate_b_verdict", None) or "block"
            return _mark_blocked(
                conn_factory,
                project,
                reason="publish_stage_gate_b_failed",
                detail=(
                    f"publish_state={result.publish_state}; "
                    f"verdict={gate_b_verdict}; {problems}"
                ),
                events=events,
                gate_job_id=gate_job_id,
                mark_status_blocked=True,
                publish_extra={
                    "gate_b_verdict": gate_b_verdict,
                    "gate_b_outcome": getattr(result, "gate_b_outcome", None),
                    "gate_b_reason_codes": codes,
                },
            )
        manifest = result.manifest or {}
        outcome = {
            "provider_video_id": result.video_id,
            "publish_state": result.publish_state,
            "idempotency_key": manifest.get("idempotency_key")
            or f"simulated-publish:{project_id}",
            "title": manifest.get("title"),
            "description": manifest.get("description") or "",
            "tags": manifest.get("tags") or [],
            "manifest_path": result.manifest_path,
            "thumbnail_artefact_id": pack.get("thumbnail_artefact_id"),
            "gate_b_verdict": getattr(result, "gate_b_verdict", None),
            "gate_b_outcome": getattr(result, "gate_b_outcome", None),
            "gate_b_reason_codes": list(
                getattr(result, "gate_b_reason_codes", None) or []),
            "compliance_gate": getattr(result, "compliance_gate", None)
            or manifest.get("compliance_gate")
            or compliance_decision,
        }
    else:
        pack_flow = "fake_provider_direct_no_pack"
        try:
            outcome = _run_direct_fake_flow(project, compliance_decision)
        except Exception as exc:  # noqa: BLE001
            return _mark_blocked(
                conn_factory,
                project,
                reason="fake_provider_error",
                detail=f"{type(exc).__name__}: {exc}",
                events=events,
                gate_job_id=gate_job_id,
                mark_status_blocked=True,
            )
        outcome["thumbnail_artefact_id"] = pack.get("thumbnail_artefact_id")

    committed = _commit_publication(
        conn_factory, project, outcome, pack_flow=pack_flow,
        compliance_gate=compliance_decision,
    )
    if not committed.get("already_finalised"):
        _emit_event(
            conn_factory,
            events,
            EVENT_PUBLISH_SIMULATED,
            {
                "project_id": project_id,
                "provider": PROVIDER,
                "simulated": SIMULATED,
                "publish_record_id": committed["publish_record_id"],
                "provider_video_id": committed.get("provider_video_id"),
                "simulated_video_id": committed.get("simulated_video_id"),
                "publish_state": committed.get("publish_state"),
                "pack_flow": pack_flow,
                "terminal_status": TERMINAL_STATUS,
                "gate_decision": resolved,
                "privacy_status": "private",
                "forced_private_by_compliance_gate": committed.get(
                    "forced_private_by_compliance_gate"
                ),
                "compliance_overall": compliance_overall,
            },
            project_id=project_id,
            job_id=gate_job_id,
        )
    return committed
