#!/usr/bin/env python3
"""Round 4 automation PROOF harness (Worker 4).

Claim under test (Round 4): a SCHEDULE TICK drives production END-TO-END
UNATTENDED through the real execution layer:

    SchedulerTick -> projects + 10-job DAG -> ExecutionService runner
    -> handlers -> integration_client -> app.integration.run_<stage>

This harness:

* creates a THROWAWAY app database + outputs root under
  ``outputs/round4/automation/`` (the dev DB is never touched),
* seeds TWO channels (A: builtin ``facts`` niche; B: an OTHER/custom
  niche built with the real Round-1 Other-niche builder, persisted
  with ``source='other'`` + ``user_idea``), one schedule per channel
  with ``publish_mode='auto'``, ``target_minutes=1`` and a due
  ``next_run_at``,
* starts the real :class:`app.execution.service.ExecutionService`
  (which owns the SchedulerTick loop + JobRunner) and then makes
  ZERO further calls — no manual approvals, no per-stage invocations,
* polls the throwaway DB until both projects reach terminal job
  states (or a stall / timeout is detected),
* copies the produced video + pack artefacts into
  ``outputs/round4/automation/<channel-slug>/`` and writes a
  machine-readable ``run_log.json`` with seed data, tick timestamps,
  per-stage timings, gate outcomes, publish states + provenance,
  per-stage stub flags and ``human_interventions: 0``.

Provider keys are read from the process environment ONLY
(``GEMINI_API_KEY`` / ``PIXABAY_API_KEY``); they are never written to
disk, logged, or printed — only their presence is recorded.

LLM tier substitution (disclosed, Round-3 precedent)
----------------------------------------------------
On this key the T1 model (``gemini-3.8-flash``) and the T2 model
(``gemini-3.1-pro-preview``) return HTTP 429 (free-tier generate
quota — verified by direct router calls before the run), while T0
``gemini-3.1-flash-lite`` works.  The model registry is DATA
(``backend/config/models.yaml``, hot-reloadable by design), so for
the duration of the proof run ONLY this harness swaps the T1/T2
Gemini entries to ``gemini-3.1-flash-lite`` **at flash-lite's own
registered prices**, and restores the original file byte-for-byte
afterwards (SHA-256 verified, both hashes recorded in the run log,
the substituted file kept in the outputs for audit).  The content
bundle's own provenance (``llm_models``) independently records the
model that actually ran — the substitution is never silent.

Usage (from the repo root)::

    GEMINI_API_KEY=... PIXABAY_API_KEY=... \
        backend/.venv/bin/python tools/round4_automation_proof.py \
        [--timeout 2400] [--stall-seconds 300]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

OUTPUT_ROOT = REPO_ROOT / "outputs" / "round4" / "automation"
STATE_DIR = OUTPUT_ROOT / "_proof_state"
DB_PATH = STATE_DIR / "app.db"
RUN_LOG_PATH = OUTPUT_ROOT / "run_log.json"

# Point every default-path resolver at the throwaway state dir BEFORE
# any app module is imported, so nothing can accidentally open the
# dev database.  The secret backend is forced to "env" so the keys
# supplied in the process environment are the ones resolved.
os.environ.setdefault("YTA_DATA_DIR", str(STATE_DIR))
os.environ["YTA_DATA_DIR"] = str(STATE_DIR)
os.environ.setdefault("YTA_SECRET_BACKEND", "env")
# The stage bridge resolves per-project work dirs from
# $YTA_PROJECTS_DIR first — keep them inside the proof outputs root.
os.environ["YTA_PROJECTS_DIR"] = str(OUTPUT_ROOT / "projects")
# Provider pacing (the LLM layer's own documented YTA_LLM_* config):
# space Gemini dispatches so the two channels' concurrent script
# calls do not 429 each other on the shared free-tier key.
os.environ.setdefault("YTA_LLM_GEMINI_MIN_INTERVAL_S", "3")
os.environ.setdefault("YTA_LLM_GEMINI_RPM", "15")

TERMINAL_JOB_STATUSES = {"done", "succeeded", "failed", "dead", "cancelled"}
CONTENT_STAGES = {
    "research", "script", "voice", "visuals", "render",
    "thumbnail", "qc", "package",
}

CHANNEL_SEEDS: list[dict[str, Any]] = [
    {
        "slug": "channel-a-facts",
        "title": "Round4 Proof — Facts",
        "youtube_channel_id": "UC-round4-proof-facts",
        "language": "ur",
        "niche": {"kind": "builtin", "key": "facts"},
        "topic": "Samundar ki Gehrai ke Raaz",
    },
    {
        "slug": "channel-b-space-mysteries",
        "title": "Round4 Proof — Space Mysteries",
        "youtube_channel_id": "UC-round4-proof-space",
        "language": "ur",
        "niche": {
            "kind": "other",
            "name": "Space Mysteries",
            "idea": (
                "Unsolved mysteries of deep space — black holes, strange "
                "signals and lost missions — explained as gripping "
                "stories in Roman Urdu for curious viewers"
            ),
        },
        "topic": "Space ke Woh Raaz Jo Ab Tak Solve Nahi Hue",
    },
]


REGISTRY_PATH = BACKEND_DIR / "config" / "models.yaml"
SUBSTITUTED_MODELS = {
    "gemini-3.8-flash": "gemini-3.1-flash-lite",       # T1
    "gemini-3.1-pro-preview": "gemini-3.1-flash-lite",  # T2
}
FLASH_LITE_PRICES = {
    "input_price_per_1m": 0.25,
    "output_price_per_1m": 1.50,
    "cached_input_price_per_1m": 0.025,
}


def now_ms() -> int:
    return int(time.time() * 1000)


def apply_registry_substitution(log: dict) -> dict:
    """Swap T1/T2 Gemini entries to flash-lite in models.yaml (data).

    Returns a state dict for :func:`restore_registry`.  The original
    bytes are kept in memory only; the substituted YAML is written to
    the outputs root for audit.
    """
    import hashlib

    import yaml

    original = REGISTRY_PATH.read_bytes()
    data = yaml.safe_load(original.decode("utf-8"))
    changed: list[dict[str, str]] = []
    for tier_key, tier_cfg in (data.get("tiers") or {}).items():
        for entry in tier_cfg.get("models") or []:
            if entry.get("provider") == "gemini" and entry.get(
                "model_id"
            ) in SUBSTITUTED_MODELS:
                changed.append({
                    "tier": tier_key,
                    "from": entry["model_id"],
                    "to": SUBSTITUTED_MODELS[entry["model_id"]],
                })
                entry["model_id"] = SUBSTITUTED_MODELS[entry["model_id"]]
                entry.update(FLASH_LITE_PRICES)
                for extra in (
                    "price_expires",
                    "input_price_after_expiry_per_1m",
                    "output_price_after_expiry_per_1m",
                ):
                    entry.pop(extra, None)
    if not changed:
        raise RuntimeError("registry substitution found no T1/T2 Gemini entries")
    substituted = yaml.safe_dump(data, sort_keys=False).encode("utf-8")
    audit_path = OUTPUT_ROOT / "models_substituted_flash_lite.yaml"
    audit_path.write_bytes(substituted)
    REGISTRY_PATH.write_bytes(substituted)
    state = {
        "applied": True,
        "reason": (
            "T1 gemini-3.8-flash and T2 gemini-3.1-pro-preview return "
            "HTTP 429 on this key (verified by direct router calls); "
            "gemini-3.1-flash-lite substituted at its own registered "
            "prices, per the Round-3 disclosed tier substitution"
        ),
        "changes": changed,
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "substituted_sha256": hashlib.sha256(substituted).hexdigest(),
        "substituted_file": str(audit_path),
        "restored": False,
        "_original_bytes": original,
    }
    log["llm_tier_substitution"] = {
        k: v for k, v in state.items() if not k.startswith("_")
    }
    return state


def restore_registry(state: Optional[dict], log: dict) -> None:
    """Restore models.yaml byte-for-byte; verify + record the hash."""
    if not state or not state.get("applied"):
        return
    import hashlib

    REGISTRY_PATH.write_bytes(state["_original_bytes"])
    restored_sha = hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    ok = restored_sha == state["original_sha256"]
    state["restored"] = ok
    log.setdefault("llm_tier_substitution", {}).update({
        "restored": ok,
        "restored_sha256": restored_sha,
    })


def iso(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _cols(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _insert(conn, table: str, values: dict[str, Any]) -> None:
    cols = [c for c in values if c in _cols(conn, table) and values[c] is not None]
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        tuple(values[c] for c in cols),
    )


def seed(conn_factory) -> list[dict[str, Any]]:
    """Seed channels + niche profiles + schedules; return seed records."""
    from app.niches.builder import build_other_profile
    from app.niches.builtins import get_profile

    ts = now_ms()
    seeds: list[dict[str, Any]] = []

    for idx, spec in enumerate(CHANNEL_SEEDS):
        # Both schedules are due when the service starts; channel B is
        # staggered +300 s (a normal scheduling choice, recorded in
        # the seed log) so the two channels' LLM/render bursts do not
        # collide on the shared free-tier key / this VM's RAM.  The
        # service's own tick materialises B unattended at that time.
        due_at = ts - 1_000 + idx * 300_000
        niche_spec = spec["niche"]
        if niche_spec["kind"] == "builtin":
            profile = get_profile(niche_spec["key"])
            niche_values: dict[str, Any] = {
                "key": profile.profile_id,
                "label": profile.name,
                "source": "builtin",
                "user_idea": None,
                "language_mix": profile.language,
                "tone": ", ".join(profile.tone),
                "words_per_minute": profile.target_wpm,
                "structure_json": json.dumps(profile.structure_template),
                "hook_style": ", ".join(profile.hook_families),
                "voice_style": profile.voice_style,
                "visual_style": profile.visual_style,
                "cta_style": profile.cta_style,
                "risk_class": (
                    "sensitive" if profile.risk_class == "sensitive" else "normal"
                ),
                "taboo_json": json.dumps(profile.taboo),
                "resolved_profile_id": profile.profile_id,
            }
        else:
            profile = build_other_profile(
                name=niche_spec["name"], idea=niche_spec["idea"]
            )
            niche_values = {
                "key": profile.profile_id,
                "label": profile.name,
                "source": "other",
                "user_idea": niche_spec["idea"],
                "language_mix": profile.language,
                "tone": ", ".join(profile.tone),
                "words_per_minute": profile.target_wpm,
                "structure_json": json.dumps(profile.structure_template),
                "hook_style": ", ".join(profile.hook_families),
                "voice_style": profile.voice_style,
                "visual_style": profile.visual_style,
                "cta_style": profile.cta_style,
                "risk_class": (
                    "sensitive" if profile.risk_class == "sensitive" else "normal"
                ),
                "taboo_json": json.dumps(profile.taboo),
                "resolved_profile_id": profile.profile_id,
            }

        niche_id = str(uuid.uuid4())
        channel_id = str(uuid.uuid4())
        schedule_id = str(uuid.uuid4())

        conn = conn_factory()
        try:
            _insert(conn, "niche_profiles", {
                "id": niche_id,
                **{k: v for k, v in niche_values.items()
                   if k != "resolved_profile_id"},
                "is_active": 1,
                "created_at": ts,
                "updated_at": ts,
            })
            _insert(conn, "channels", {
                "id": channel_id,
                "youtube_channel_id": spec["youtube_channel_id"],
                "title": spec["title"],
                "default_niche_profile_id": niche_id,
                "language": spec["language"],
                "status": "active",
                "created_at": ts,
                "updated_at": ts,
            })
            cadence = {
                "type": "interval",
                "interval_hours": 24,
                "topics": [spec["topic"]],
            }
            _insert(conn, "schedules", {
                "id": schedule_id,
                "name": f"Round4 proof schedule — {spec['slug']}",
                "channel_id": channel_id,
                "niche_profile_id": niche_id,
                "cadence_json": json.dumps(cadence),
                "topic_mode": "fixed_list",
                "target_minutes": 1,
                "publish_mode": "auto",
                "is_enabled": 1,
                "next_run_at": due_at,
                "created_at": ts,
                "updated_at": ts,
            })
        finally:
            conn.close()

        seeds.append({
            "channel_slug": spec["slug"],
            "channel_id": channel_id,
            "youtube_channel_id": spec["youtube_channel_id"],
            "channel_title": spec["title"],
            "niche_profile_id": niche_id,
            "niche_key": niche_values["key"],
            "niche_label": niche_values["label"],
            "niche_source": niche_values["source"],
            "niche_user_idea": niche_values.get("user_idea"),
            "resolved_profile_id": niche_values["resolved_profile_id"],
            "schedule_id": schedule_id,
            "schedule": {
                "publish_mode": "auto",
                "topic_mode": "fixed_list",
                "target_minutes": 1,
                "cadence": cadence,
                "next_run_at": due_at,
                "next_run_at_iso": iso(due_at),
            },
            "topic": spec["topic"],
        })
    return seeds


# ---------------------------------------------------------------------------
# DB reading helpers
# ---------------------------------------------------------------------------


def _rows(conn_factory, sql: str, params: tuple = ()) -> list[dict]:
    conn = conn_factory()
    try:
        conn.row_factory = __import__("sqlite3").Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _json_or_none(raw: Any) -> Any:
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _find_keys(obj: Any, wanted: set[str], prefix: str = "") -> dict[str, Any]:
    """Recursively collect scalar values for ``wanted`` keys (first hit)."""
    found: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in wanted and key not in found and not isinstance(
                value, (dict, list)
            ):
                found[key] = value
            if isinstance(value, (dict, list)):
                for k, v in _find_keys(value, wanted, prefix).items():
                    found.setdefault(k, v)
    elif isinstance(obj, list):
        for item in obj:
            for k, v in _find_keys(item, wanted, prefix).items():
                found.setdefault(k, v)
    return found


def _all_stub_flags(obj: Any) -> list[bool]:
    flags: list[bool] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "stub" and isinstance(value, bool):
                flags.append(value)
            else:
                flags.extend(_all_stub_flags(value))
    elif isinstance(obj, list):
        for item in obj:
            flags.extend(_all_stub_flags(item))
    return flags


# ---------------------------------------------------------------------------
# Run + collect
# ---------------------------------------------------------------------------


async def run_service_until_terminal(
    conn_factory, timeout_s: float, stall_s: float, log: dict
) -> dict:
    """Start the real ExecutionService; poll (read-only) until terminal."""
    from app.execution.service import build_service

    service = build_service(
        conn_factory,
        scheduler_interval_s=5.0,
        scheduler_initial_delay_s=1.0,
    )
    log["service"] = {
        "registry_kinds": sorted(service.registry.keys()),
        "runner_present": service.runner is not None,
        "scheduler_present": service._scheduler is not None,
    }
    started = time.monotonic()
    log["tick"] = {"service_started_at": iso(now_ms())}
    await service.start()
    log["service"]["status_after_start"] = service.status()

    last_change = time.monotonic()
    last_snapshot = ""
    outcome = "timeout"
    try:
        while time.monotonic() - started < timeout_s:
            await asyncio.sleep(2.0)
            projects = _rows(conn_factory, "SELECT * FROM projects ORDER BY created_at")
            jobs = _rows(conn_factory, "SELECT * FROM jobs ORDER BY created_at")
            snapshot = json.dumps(
                [(j["id"], j["status"]) for j in jobs]
                + [(p["id"], p["status"]) for p in projects],
                sort_keys=True,
            )
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                last_change = time.monotonic()
            if len(projects) >= len(CHANNEL_SEEDS):
                for p in projects:
                    key = f"project_materialised_at:{p['id']}"
                    if key not in log["tick"]:
                        log["tick"][key] = iso(p["created_at"])
                if jobs and all(
                    j["status"] in TERMINAL_JOB_STATUSES for j in jobs
                ):
                    outcome = "all_jobs_terminal"
                    break
            if (
                projects
                and time.monotonic() - last_change > stall_s
            ):
                outcome = "stalled_no_job_progress"
                break
    finally:
        await service.stop()
    log["tick"]["service_stopped_at"] = iso(now_ms())
    log["tick"]["wall_clock_s"] = round(time.monotonic() - started, 1)
    log["tick"]["outcome"] = outcome
    return {"outcome": outcome}


def collect_project(conn_factory, seed_rec: dict, run_started_ms: int) -> dict:
    project_rows = _rows(
        conn_factory,
        "SELECT * FROM projects WHERE channel_id = ? ORDER BY created_at",
        (seed_rec["channel_id"],),
    )
    entry: dict[str, Any] = {"seed": seed_rec, "projects_found": len(project_rows)}
    if not project_rows:
        entry["final_status"] = "no_project_materialised"
        return entry
    project = project_rows[-1]
    pid = project["id"]
    entry["project_id"] = pid
    entry["project"] = {
        k: project.get(k)
        for k in (
            "topic", "topic_source", "target_minutes", "target_words",
            "status", "current_phase", "publish_mode", "schedule_id",
            "youtube_video_id", "published_at", "total_cost_micros",
            "error_summary", "created_at", "updated_at",
        )
    }
    entry["project"]["gate_status"] = _json_or_none(project.get("gate_status_json"))
    entry["final_status"] = project.get("status")

    jobs = _rows(
        conn_factory,
        "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at",
        (pid,),
    )
    stage_timings: list[dict[str, Any]] = []
    job_records: list[dict[str, Any]] = []
    results_by_kind: dict[str, Any] = {}
    for job in jobs:
        result = _json_or_none(job.get("result_json"))
        if isinstance(result, dict):
            results_by_kind[job["kind"]] = result
        duration_s = None
        if job.get("created_at") and job.get("updated_at"):
            duration_s = round((job["updated_at"] - job["created_at"]) / 1000.0, 2)
        rec = {
            "kind": job["kind"],
            "status": job["status"],
            "attempts": job.get("attempts"),
            "created_at": iso(job.get("created_at")),
            "updated_at": iso(job.get("updated_at")),
            "duration_s_incl_queue_wait": duration_s,
            "error_code": job.get("error_code"),
            "error": (job.get("error") or "")[:500] or None,
        }
        job_records.append(rec)
        stage_timings.append(
            {"stage": job["kind"], "duration_s": duration_s, "status": job["status"]}
        )
    entry["jobs"] = job_records
    entry["stage_timings"] = stage_timings

    runs = _rows(
        conn_factory,
        "SELECT * FROM agent_runs WHERE project_id = ? ORDER BY created_at",
        (pid,),
    )
    run_records = []
    for run in runs:
        out = _json_or_none(run.get("output_ref_json"))
        run_records.append({
            "agent_key": run.get("agent_key"),
            "status": run.get("status"),
            "model": run.get("model"),
            "provider": run.get("provider"),
            "cost_micros": run.get("cost_micros"),
            "output": out,
        })
    entry["agent_runs"] = run_records

    # Stub audit + provenance, per stage (from job results, falling
    # back to agent-run outputs).
    stub_audit: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    stage_names = {
        "stage.research": "research", "stage.script": "script",
        "stage.voice": "voice", "stage.visuals": "visuals",
        "stage.render": "render", "stage.thumbnail": "thumbnail",
        "stage.qc": "qc", "stage.package": "package",
        "gate.a": "gate_a", "gate.b": "gate_b",
    }
    for kind, stage in stage_names.items():
        result = results_by_kind.get(kind)
        if result is None:
            for run in run_records:
                if run["agent_key"] == kind and isinstance(run["output"], dict):
                    result = run["output"]
                    break
        if not isinstance(result, dict):
            stub_audit[stage] = {"result_present": False, "stub": None}
            continue
        flags = _all_stub_flags(result)
        stub_audit[stage] = {
            "result_present": True,
            "stub_flags_found": flags,
            "stub": (True if any(flags) else False if flags else
                     bool(result.get("stub", False)) if "stub" in result else None),
        }
        prov = _find_keys(result, {
            "llm_path", "llm_models", "path_used", "paths_used",
            "publish_state", "publish_provider", "provider",
            "qc_passed", "gate_b_passed", "gate_a_passed",
            "subtitle_path_used", "render_path_used", "model",
            "file_path", "final_mp4", "pack_dir",
        })
        if isinstance(result.get("provenance"), dict):
            prov["provenance"] = result["provenance"]
        if prov:
            provenance[stage] = prov
    entry["stub_audit"] = stub_audit
    entry["provenance"] = provenance
    content_stub = {
        s: stub_audit[s]["stub"]
        for s in CONTENT_STAGES
        if s in stub_audit and stub_audit[s]["result_present"]
    }
    entry["content_stage_stub_flags"] = content_stub
    entry["all_content_stages_real"] = bool(content_stub) and all(
        v is False for v in content_stub.values()
    )

    # Gates: project gate_status + approvals rows + gate job results.
    approvals: list[dict] = []
    try:
        approvals = _rows(
            conn_factory,
            "SELECT * FROM approvals WHERE project_id = ? ORDER BY created_at",
            (pid,),
        )
    except Exception:
        approvals = []
    entry["gates"] = {
        "project_gate_status": entry["project"].get("gate_status"),
        "gate_a_result": results_by_kind.get("gate.a"),
        "gate_b_result": results_by_kind.get("gate.b"),
        "approvals": [
            {k: a.get(k) for k in (
                "id", "gate", "status", "decision", "reason_code", "note",
                "created_at", "decided_at",
            ) if k in a}
            for a in approvals
        ],
    }

    # Publish: project fields + package result + any publish job +
    # the publish_finaliser's publish_records row + gate_status_json
    # publish provenance (provider='fake', simulated=True expected).
    publish_records: list[dict] = []
    try:
        publish_records = _rows(
            conn_factory,
            "SELECT * FROM publish_records WHERE project_id = ?",
            (pid,),
        )
    except Exception:
        publish_records = []
    entry["publish"] = {
        "project_status": project.get("status"),
        "youtube_video_id": project.get("youtube_video_id"),
        "published_at": iso(project.get("published_at")),
        "package_result": results_by_kind.get("stage.package"),
        "publish_job_results": {
            k: v for k, v in results_by_kind.items() if "publish" in k
        },
        "gate_status_publish_provenance": (
            entry["project"].get("gate_status") or {}
        ).get("publish") if isinstance(entry["project"].get("gate_status"), dict) else None,
        "publish_records": publish_records,
    }

    artefacts = _rows(
        conn_factory,
        "SELECT * FROM project_artefacts WHERE project_id = ? ORDER BY created_at",
        (pid,),
    )
    entry["artefacts"] = [
        {
            "artefact_type": a.get("artefact_type"),
            "version": a.get("version"),
            "file_path": a.get("file_path"),
            "exists_on_disk": (
                Path(a["file_path"]).is_file()
                if a.get("file_path") and not a["file_path"].startswith("stub://")
                else False
            ),
        }
        for a in artefacts
    ]
    entry["_results_by_kind"] = results_by_kind
    entry["_run_started_ms"] = run_started_ms
    return entry


def copy_pack(entry: dict, slug_dir: Path) -> dict:
    """Copy the produced video + pack artefacts into ``slug_dir``."""
    slug_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[Path] = []

    def _add(path_like: Any) -> None:
        if isinstance(path_like, str) and path_like and "://" not in path_like:
            p = Path(path_like)
            if p.is_file():
                candidates.append(p)
            elif p.is_dir():
                candidates.extend(f for f in p.rglob("*") if f.is_file())

    for art in entry.get("artefacts", []):
        _add(art.get("file_path"))
    # Hunt through stage results for output paths (final_mp4, pack_dir…).
    wanted_suffixes = {
        ".mp4", ".srt", ".jpg", ".jpeg", ".png", ".md", ".csv", ".json",
    }

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)
        elif isinstance(obj, str) and "://" not in obj:
            # Stage results embed long prose (script text etc.) — only
            # short, single-line strings can be paths; stat() on a
            # >255-char name raises OSError, so guard before touching FS.
            if len(obj) > 240 or "\n" in obj:
                return
            p = Path(obj)
            # Stock cache assets are NOT pack artefacts: skip anything
            # under a stock/ dir and hash-named media (the stock cache
            # naming scheme) so copied packs never masquerade raw
            # source clips as the produced video.
            if "/stock/" in obj.replace("\\", "/"):
                return
            if p.suffix.lower() == ".mp4" and len(p.stem) == 64 and all(
                ch in "0123456789abcdef" for ch in p.stem.lower()
            ):
                return
            try:
                if p.suffix.lower() in wanted_suffixes and p.is_file():
                    candidates.append(p)
                elif p.is_dir():
                    for f in p.rglob("*"):
                        if "/stock/" in str(f).replace("\\", "/"):
                            continue
                        if f.is_file() and f.suffix.lower() in wanted_suffixes:
                            candidates.append(f)
            except OSError:
                return

    _walk(entry.get("_results_by_kind", {}))

    copied: list[dict[str, Any]] = []
    seen: set[str] = set()
    for src in candidates:
        key = str(src.resolve())
        if key in seen:
            continue
        seen.add(key)
        # Keep the pack flat unless two files share a name.
        dest = slug_dir / src.name
        if dest.exists() and dest.resolve() != src.resolve():
            dest = slug_dir / f"{src.parent.name}_{src.name}"
        try:
            if src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
            copied.append({
                "file": dest.name,
                "source": str(src),
                "bytes": dest.stat().st_size,
            })
        except Exception as exc:  # noqa: BLE001
            copied.append({"file": src.name, "source": str(src), "error": str(exc)})
    copied.sort(key=lambda c: c["file"])
    return {"dir": str(slug_dir), "files": copied}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--stall-seconds", type=float, default=900.0)
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Skip wipe/seed/run; regenerate run_log.json + copied "
             "packs from the existing proof DB/outputs.",
    )
    args = parser.parse_args(argv)

    run_started_ms = now_ms()
    log: dict[str, Any] = {
        "proof": "round4_automation",
        "generated_at": iso(run_started_ms),
        "harness": str(Path(__file__).resolve()),
        "db_path": str(DB_PATH),
        "outputs_root": str(OUTPUT_ROOT),
        "env_keys_present": {
            "GEMINI_API_KEY": bool(os.environ.get("GEMINI_API_KEY")),
            "PIXABAY_API_KEY": bool(os.environ.get("PIXABAY_API_KEY")),
        },
        "human_interventions": 0,
    }

    # Bridge presence audit (checked again at collection time).
    try:
        import app.integration as integration_pkg

        log["bridge_at_start"] = {
            "run_functions": sorted(
                name for name in dir(integration_pkg) if name.startswith("run_")
            ),
            "stage_bridge_module": (
                __import__("importlib").util.find_spec(
                    "app.integration.stage_bridge"
                )
                is not None
            ),
        }
    except Exception as exc:  # noqa: BLE001
        log["bridge_at_start"] = {"error": f"{type(exc).__name__}: {exc}"}

    from app.db import get_connection, init_db

    def conn_factory():
        return get_connection(DB_PATH)

    if args.collect_only:
        # Rebuild seed records from the existing proof DB (creation
        # order = seed order) and re-collect without touching the run.
        channels = _rows(
            conn_factory, "SELECT * FROM channels ORDER BY created_at"
        )
        seeds = []
        for idx, ch in enumerate(channels):
            niche_rows = _rows(
                conn_factory,
                "SELECT * FROM niche_profiles WHERE id = ?",
                (ch["default_niche_profile_id"],),
            )
            niche = niche_rows[0] if niche_rows else {}
            sched_rows = _rows(
                conn_factory,
                "SELECT * FROM schedules WHERE channel_id = ? "
                "ORDER BY created_at",
                (ch["id"],),
            )
            sched = sched_rows[0] if sched_rows else {}
            spec = CHANNEL_SEEDS[idx] if idx < len(CHANNEL_SEEDS) else {}
            seeds.append({
                "channel_slug": spec.get("slug", f"channel-{idx}"),
                "channel_id": ch["id"],
                "youtube_channel_id": ch.get("youtube_channel_id"),
                "channel_title": ch.get("title"),
                "niche_profile_id": ch.get("default_niche_profile_id"),
                "niche_key": niche.get("key"),
                "niche_label": niche.get("label"),
                "niche_source": niche.get("source"),
                "niche_user_idea": niche.get("user_idea"),
                "schedule_id": sched.get("id"),
                "collect_only_recollection": True,
            })
        log["seeds"] = seeds
        log["collect_only"] = True
        # Carry the original run's audit sections forward verbatim.
        if RUN_LOG_PATH.is_file():
            try:
                previous = json.loads(RUN_LOG_PATH.read_text(encoding="utf-8"))
                for key in (
                    "llm_tier_substitution", "tick", "bridge_at_start",
                    "bridge_at_end", "seeded_at", "schedule_runs",
                ):
                    if key in previous:
                        log[key] = previous[key]
                log["run_outcome_original"] = previous.get("run_outcome")
            except Exception:
                pass
        run = {"outcome": "collect_only"}
        log["run_outcome"] = run["outcome"]
        # Clean the per-channel copy dirs (earlier copies may include
        # files the fixed copier now excludes).
        for seed_rec in seeds:
            slug_dir = OUTPUT_ROOT / seed_rec["channel_slug"]
            if slug_dir.exists():
                shutil.rmtree(slug_dir)
    else:
        # Fresh throwaway state + outputs.
        if OUTPUT_ROOT.exists():
            shutil.rmtree(OUTPUT_ROOT)
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        conn = init_db(DB_PATH)
        conn.close()

        seeds = seed(conn_factory)
        log["seeds"] = seeds
        log["seeded_at"] = iso(now_ms())

        registry_state: Optional[dict] = None
        try:
            if log["env_keys_present"]["GEMINI_API_KEY"]:
                registry_state = apply_registry_substitution(log)
            run = asyncio.run(
                run_service_until_terminal(
                    conn_factory, args.timeout, args.stall_seconds, log
                )
            )
        finally:
            restore_registry(registry_state, log)
        log["run_outcome"] = run["outcome"]

    # Schedule runs (tick evidence).
    try:
        log["schedule_runs"] = _rows(
            conn_factory,
            "SELECT schedule_id, project_id, status, reason, detail_json, "
            "started_at, finished_at FROM schedule_runs ORDER BY created_at",
        )
    except Exception as exc:  # noqa: BLE001
        log["schedule_runs"] = {"error": str(exc)}

    # Bridge presence re-audit.
    try:
        import importlib

        importlib.invalidate_caches()
        import app.integration as integration_pkg

        log["bridge_at_end"] = {
            "run_functions": sorted(
                name for name in dir(integration_pkg) if name.startswith("run_")
            ),
            "stage_bridge_module": (
                importlib.util.find_spec("app.integration.stage_bridge")
                is not None
            ),
        }
    except Exception as exc:  # noqa: BLE001
        log["bridge_at_end"] = {"error": f"{type(exc).__name__}: {exc}"}

    projects_log: dict[str, Any] = {}
    for seed_rec in seeds:
        entry = collect_project(conn_factory, seed_rec, run_started_ms)
        pack = copy_pack(entry, OUTPUT_ROOT / seed_rec["channel_slug"])
        entry["copied_pack"] = pack
        videos = [f for f in pack["files"] if f["file"].lower().endswith(".mp4")]
        entry["videos"] = videos
        entry.pop("_results_by_kind", None)
        entry.pop("_run_started_ms", None)
        projects_log[seed_rec["channel_slug"]] = entry
    log["projects"] = projects_log
    log["finished_at"] = iso(now_ms())

    RUN_LOG_PATH.write_text(
        json.dumps(log, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"run_log: {RUN_LOG_PATH}")
    print(f"outcome: {run['outcome']}")
    for slug, entry in projects_log.items():
        print(
            f"{slug}: status={entry.get('final_status')} "
            f"videos={[v['file'] for v in entry.get('videos', [])]} "
            f"all_content_real={entry.get('all_content_stages_real')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
