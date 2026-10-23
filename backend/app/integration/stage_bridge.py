"""Real stage bridge for the execution DAG (Round 4, Worker 1).

Closes the Round-2 gap where ``app.integration`` exported no
``run_<stage>`` functions, so every scheduled stage silently fell back
to the deterministic stub artefact in
:mod:`app.execution.handlers`.  This module implements the eight
non-gate DAG stages by driving the REAL stage machinery that already
exists in this package, incrementally, one project at a time::

    DAG stage   real machinery (this package)
    ---------   ------------------------------------------------------
    research    topic resolution (``auto:<niche>`` sentinel ->
                deterministic ideation from the resolved profile) +
                research brief built from ``content`` helpers
                (``_keywords`` / ``_plan_sections`` / ``_budgets``)
    script      ``content.build_content_live`` via the run-loop owner
                (real Gemini when a key resolves — provenance
                ``gemini-live`` — else the content layer's own
                provenance-marked deterministic bundle)
    voice       ``voice_stage.synthesize_voice`` (real edge-tts with
                its own per-chapter stub fallback + chapter cache)
    visuals     ``visuals_stage.plan_and_generate_visuals``
                (stock-first Pixabay, local-plate fallback)
    render      ``assemble_stage.assemble_project`` (segment render +
                real-Whisper subtitles + thumbnails + QC) — the
                render/thumbnail/qc DAG stages share ONE assemble run
    thumbnail   the thumbnail variants of that same assemble run
    qc          the technical-QC verdict of that same assemble run
    package     ``publish_stage.finalize_publish`` (Gate B tokens,
                frozen manifest, fake-provider workflow) + the pack
                writers (licence ledger CSV + ``report.md``)

Contract (see :mod:`app.execution.integration_client` /
:mod:`app.execution.handlers`): each ``run_<stage>(project, payload)``
receives the ``projects`` row as a plain dict and the job payload, and
returns a dict result (``None`` would mean "fall back to stub" — this
bridge returns real results, and lets genuine errors propagate to the
handler, which owns the stub-fallback policy).  Results carry an
``artefact`` dict, a ``file_path`` where a real file exists, cost in
``cost_usd``/``cost_micros`` when a stage actually incurred it, and
mandatory ``provenance`` — what ACTUALLY ran (``llm_path``, TTS path,
stock vs plate visuals, whisper vs alignment subtitles, publish
provider).  Nothing here fabricates a "real" label: the content
stage's own provenance string is passed through verbatim.

Per-project state / idempotency
-------------------------------
Each project gets a working directory ``<base>/<project_id>/`` built
by :func:`app.integration.models.make_context` (``content/``,
``audio/``, ``visuals/``, ``render/``, ``pack/``).  ``base`` resolves
from, in order: ``payload["base_dir"]``, ``project["base_dir"]``,
``$YTA_PROJECTS_DIR``, ``$YTA_DATA_DIR/projects``, the app settings
data dir, else ``<cwd>/outputs/projects``.

Every stage persists its output as a JSON checkpoint inside that
layout (``content/bundle.json``, ``audio/voice.json``,
``visuals/visuals.json``, ``render/assemble.json``,
``pack/package.json``, …) plus a ``bridge_state.json`` index.
Re-running a stage whose checkpoint (and headline file, where one
exists) is present returns the cached result with ``cached: True``
and does NOT redo the expensive stage call — stages are resumable
after a crash and safe under job retries.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

__all__ = [
    "run_research",
    "run_script",
    "run_voice",
    "run_visuals",
    "run_render",
    "run_thumbnail",
    "run_qc",
    "run_package",
    "get_base_dir",
    "resolve_profile_for_project",
    "project_to_spec",
]

# ---------------------------------------------------------------------------
# Monkeypatch points (produce.py pattern): tests / callers may set any of
# these module globals to a callable; ``None`` means "resolve the real
# stage function from its owning module on every call".
# ---------------------------------------------------------------------------
build_content_auto = None  # type: ignore[assignment]
synthesize_voice = None  # type: ignore[assignment]
plan_and_generate_visuals = None  # type: ignore[assignment]
assemble_project = None  # type: ignore[assignment]
finalize_publish = None  # type: ignore[assignment]
run_gate_a = None  # type: ignore[assignment]

_STAGE_MODULES = {
    "build_content_auto": "app.integration.content",
    "synthesize_voice": "app.integration.voice_stage",
    "plan_and_generate_visuals": "app.integration.visuals_stage",
    "assemble_project": "app.integration.assemble_stage",
    "finalize_publish": "app.integration.publish_stage",
    "run_gate_a": "app.integration.content",
}


def _stage_fn(name: str):
    override = globals().get(name)
    if callable(override):
        return override
    import importlib

    module = importlib.import_module(_STAGE_MODULES[name])
    fn = getattr(module, name)
    if not callable(fn):
        raise TypeError(f"{_STAGE_MODULES[name]}.{name} is not callable")
    return fn


# ---------------------------------------------------------------------------
# Small generic helpers
# ---------------------------------------------------------------------------

def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if obj is None:
            break
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None and value != "":
            return value
    return default


def _to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump(mode="json"))
        except Exception:
            try:
                return dict(dump())
            except Exception:
                pass
    if is_dataclass(obj) and not isinstance(obj, type):
        try:
            return dict(asdict(obj))
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return dict(vars(obj))
    return {}


def _write_json(path: Path, data: Any) -> Path:
    from app.integration.pack import write_json

    return write_json(path, data)


def _read_json(path: Path) -> Optional[dict]:
    try:
        if path.is_file():
            parsed = json.loads(path.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Base dir / project identity
# ---------------------------------------------------------------------------

def get_base_dir(project: dict, payload: Optional[dict] = None) -> Path:
    """Resolve the per-project working-tree base (see module docstring)."""
    payload = payload or {}
    for source in (payload, project):
        for key in ("base_dir", "work_base_dir", "projects_base", "work_dir"):
            value = _get(source, key)
            if value:
                return Path(str(value)).expanduser()
    env_dir = os.environ.get("YTA_PROJECTS_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    data_dir = os.environ.get("YTA_DATA_DIR")
    if data_dir:
        return Path(data_dir).expanduser() / "projects"
    try:
        from app.config import get_settings

        return Path(get_settings().data_dir) / "projects"
    except Exception:
        pass
    return Path.cwd() / "outputs" / "projects"


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_project_id(project: dict) -> str:
    raw = str(_get(project, "id", "project_id", default="project") or "project")
    if "/" not in raw and "\\" not in raw and raw.strip() == raw and raw:
        return raw
    cleaned = _SAFE_ID_RE.sub("-", raw).strip("-.") or "project"
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{suffix}"


# ---------------------------------------------------------------------------
# Profile resolution: projects.niche_profile_id is a DB row id (UUID),
# while the pipeline works with niches.NicheProfile objects.  Resolution
# order: explicit payload hints -> DB row (builtin key / OTHER builder /
# row fields) -> the id itself as a builtin key -> facts fallback
# (recorded honestly in the returned notes).
# ---------------------------------------------------------------------------

def _try_db_rows(project: dict, payload: dict) -> tuple[Optional[dict], Optional[dict]]:
    """Best-effort (niche_row, channel_row) lookup; never raises."""
    import sqlite3

    candidates: list[Path] = []
    for source in (payload, project):
        for key in ("db_path", "database_path"):
            value = _get(source, key)
            if value:
                candidates.append(Path(str(value)))
    try:
        from app.config import get_settings

        candidates.append(Path(get_settings().db_path))
    except Exception:
        pass
    try:
        from app.db import default_db_path

        candidates.append(Path(default_db_path()))
    except Exception:
        pass

    niche_id = _get(project, "niche_profile_id", "niche_id")
    channel_id = _get(project, "channel_id")
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not path.is_file():
                continue
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                niche_row = None
                channel_row = None
                if niche_id:
                    row = conn.execute(
                        "SELECT * FROM niche_profiles WHERE id = ?", (niche_id,)
                    ).fetchone()
                    if row is not None:
                        niche_row = dict(row)
                if channel_id:
                    row = conn.execute(
                        "SELECT * FROM channels WHERE id = ?", (channel_id,)
                    ).fetchone()
                    if row is not None:
                        channel_row = dict(row)
                if niche_row is not None or channel_row is not None:
                    return niche_row, channel_row
            finally:
                conn.close()
        except Exception:
            continue
    return None, None


def _json_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        return [part.strip() for part in text.split(",") if part.strip()]
    return []


def _profile_from_row_fields(row: dict, base_profile: Any) -> Any:
    """Build a NicheProfile from a DB row over a valid base profile."""
    from app.niches.models import NicheProfile

    data = base_profile.model_dump()
    key = str(row.get("key") or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") or "custom"
    if not re.match(r"^[a-z]", slug):
        slug = f"n_{slug}"
    data["profile_id"] = slug[:60]
    if row.get("label"):
        data["name"] = str(row["label"])[:120]
    data["source"] = "other" if row.get("source") == "other" else "builtin"
    if row.get("language_mix"):
        data["language"] = str(row["language_mix"])
    tone = _json_list(row.get("tone"))
    if tone:
        cleaned = [str(t).strip() for t in tone if str(t).strip()]
        while len(cleaned) < 3:
            cleaned.append(base_profile.tone[len(cleaned) % len(base_profile.tone)])
        data["tone"] = cleaned[:3]
    if row.get("words_per_minute"):
        try:
            data["target_wpm"] = int(row["words_per_minute"])
        except (TypeError, ValueError):
            pass
    structure = _json_list(row.get("structure_json"))
    if structure:
        sections = [str(s).strip() for s in structure if str(s).strip()]
        if sections and "hook" not in sections[0].lower():
            sections.insert(0, "Hook")
        if sections and "conclusion" not in sections[-1].lower():
            sections.append("Conclusion + CTA")
        if len(sections) >= 2:
            data["structure_template"] = sections
    for field, column in (
        ("voice_style", "voice_style"),
        ("visual_style", "visual_style"),
        ("cta_style", "cta_style"),
    ):
        if row.get(column):
            data[field] = str(row[column])
    taboo = _json_list(row.get("taboo_json"))
    if taboo:
        data["taboo"] = [str(t) for t in taboo]
    return NicheProfile(**{k: v for k, v in data.items() if k in NicheProfile.model_fields})


def resolve_profile_for_project(
    project: dict, payload: Optional[dict] = None
) -> tuple[Any, str]:
    """Return ``(NicheProfile, resolution_note)`` for a project row."""
    payload = payload or {}
    from app.niches.builtins import BUILTIN_PROFILES, get_profile

    # 1. Explicit hints on payload / project.
    for source in (payload, project):
        hint = _get(source, "niche_key", "niche", "profile_id", "niche_id_key")
        if isinstance(hint, str) and hint.strip().lower() in BUILTIN_PROFILES:
            key = hint.strip().lower()
            return get_profile(key), f"payload/project hint -> builtin '{key}'"

    # 2. DB row for projects.niche_profile_id.
    niche_row, _channel_row = _try_db_rows(project, payload)
    if niche_row is not None:
        key = str(niche_row.get("key") or "").strip().lower()
        if key in BUILTIN_PROFILES:
            return get_profile(key), f"niche_profiles.key -> builtin '{key}'"
        if niche_row.get("source") == "other":
            label = str(niche_row.get("label") or "Custom niche").strip()
            idea = str(niche_row.get("user_idea") or "").strip()
            if len(idea) < 10:
                idea = f"{label} — videos, stories and analysis in depth"
            try:
                from app.niches.builder import build_other_profile

                profile = build_other_profile(name=label[:40], idea=idea[:500])
                return profile, "niche_profiles row (source=other) -> build_other_profile"
            except Exception:
                pass  # fall through to row-fields construction
        base = get_profile("facts")
        try:
            return (
                _profile_from_row_fields(niche_row, base),
                "niche_profiles row fields -> NicheProfile (over facts base)",
            )
        except Exception:
            return base, "niche_profiles row unusable -> facts fallback"

    # 3. The id itself may BE a builtin key (hand-built project dicts).
    raw_id = str(_get(project, "niche_profile_id", "niche_id", default="") or "")
    if raw_id.strip().lower() in BUILTIN_PROFILES:
        key = raw_id.strip().lower()
        return get_profile(key), f"niche_profile_id is builtin key '{key}'"

    # 4. Honest fallback.
    return (
        get_profile("facts"),
        f"facts fallback (niche_profile_id {raw_id!r} not resolvable "
        "to a builtin profile or DB row)",
    )


# ---------------------------------------------------------------------------
# Project row -> ProjectSpec + StageContext
# ---------------------------------------------------------------------------

def _target_minutes(project: dict, payload: dict, profile: Any) -> float:
    raw = _get(payload, "target_minutes", "minutes") or _get(
        project, "target_minutes", "minutes"
    )
    if raw:
        try:
            minutes = float(raw)
            if minutes > 0:
                return minutes
        except (TypeError, ValueError):
            pass
    words = _get(project, "target_words")
    if words:
        try:
            wpm = int(getattr(profile, "target_wpm", 0) or 140)
            minutes = float(words) / max(60, wpm)
            if minutes > 0:
                return round(minutes, 3)
        except (TypeError, ValueError):
            pass
    return 1.0


def project_to_spec(
    project: dict,
    payload: Optional[dict] = None,
    profile: Any = None,
    *,
    topic_override: Optional[str] = None,
) -> Any:
    """Map a ``projects`` row to a :class:`ProjectSpec` honestly."""
    from app.integration.models import ProjectSpec

    payload = payload or {}
    if profile is None:
        profile, _note = resolve_profile_for_project(project, payload)
    _niche_row, channel_row = _try_db_rows(project, payload)

    topic = topic_override or str(
        _get(payload, "topic") or _get(project, "topic", default="") or ""
    ).strip()
    if not topic:
        topic = str(getattr(profile, "name", "Facts"))

    # Niche identity for the spec: builtin key when the resolved
    # profile is builtin, else "other" + the profile's own name/idea.
    profile_id = str(getattr(profile, "profile_id", "") or "")
    source = str(getattr(profile, "source", "") or "")
    if source == "other" or profile_id.startswith("other_"):
        niche = "other"
        custom_name = str(getattr(profile, "name", "") or "custom niche")
        idea = None
        if channel_row is None:
            pass
        niche_row, _ = _try_db_rows(project, payload)
        if niche_row is not None and niche_row.get("user_idea"):
            idea = str(niche_row["user_idea"])
        idea = idea or f"{custom_name} videos in depth"
    else:
        niche = profile_id or "facts"
        custom_name = None
        idea = None

    # Language: channel language wins when explicit, else profile.
    language = "ur"
    channel_language = None
    if channel_row is not None:
        channel_language = channel_row.get("language")
    channel_language = channel_language or _get(project, "channel_language", "language")
    hinted = _get(payload, "language") or channel_language
    if isinstance(hinted, str) and hinted.strip():
        low = hinted.strip().lower()
        if low.startswith("en"):
            language = "en"
        elif low.startswith("ur"):
            language = "ur"
    elif str(getattr(profile, "language", "")).strip().lower() == "english":
        language = "en"

    # Quality tier (Round 5): a PER-CHANNEL setting
    # (``channels.quality_tier``), resolved here onto the spec — this
    # is the seam for BOTH production entry paths, because manual
    # projects and scheduler-materialised projects both reach the
    # stages through this module with the project's channel row in
    # the DB.  Precedence: an explicit payload value, then the
    # channel context the execution handlers resolved for this
    # project (``channel_context.quality_tier``), then the channel
    # row itself, then the ``draft`` default.  An unknown value
    # raises (fail loud, via app.channel_quality) rather than
    # silently rendering a draft.
    from app.channel_quality import normalize_quality_tier

    _ctx_tier = None
    for _source in (payload, project):
        _cc = _get(_source, "channel_context")
        if isinstance(_cc, dict) and _cc.get("quality_tier"):
            _ctx_tier = _cc["quality_tier"]
            break
    quality_tier = normalize_quality_tier(
        _get(payload, "quality_tier")
        or _get(project, "quality_tier")
        or _ctx_tier
        or (channel_row or {}).get("quality_tier")
    )

    voice_id = (
        _get(payload, "voice_id")
        or _get(project, "voice_id")
        or None
    )
    if not voice_id and channel_row is None:
        pass
    if not voice_id:
        niche_row, _ = _try_db_rows(project, payload)
        if niche_row is not None and niche_row.get("default_voice_id"):
            voice_id = str(niche_row["default_voice_id"])

    return ProjectSpec(
        niche=niche,
        custom_niche_name=custom_name,
        niche_idea=idea,
        topic=topic,
        minutes=_target_minutes(project, payload, profile),
        language=language,
        voice_id=str(voice_id) if voice_id else None,
        channel_id=str(_get(project, "channel_id", default="local-test-channel")
                       or "local-test-channel"),
        quality_tier=quality_tier,
        publish_mode=str(
            _get(project, "publish_mode", default="review_first") or "review_first"
        ),
        seed=42,
    )


def _resolve_context(
    project: dict, payload: Optional[dict] = None, *, topic_override: Optional[str] = None
) -> tuple[Any, Any, Any, str]:
    """(spec, profile, ctx, profile_note) for one project row."""
    from app.integration.models import make_context
    from app.integration.produce import resolve_profile as produce_resolve_profile

    payload = payload or {}
    profile, note = resolve_profile_for_project(project, payload)
    spec = project_to_spec(project, payload, profile, topic_override=topic_override)
    # Keep the DB-resolved profile authoritative: produce.resolve_profile
    # would rebuild an OTHER profile from name/idea (losing DB fields);
    # only use it when our resolution fell back for a builtin-shaped spec.
    if str(getattr(profile, "source", "")) != "other":
        try:
            candidate = produce_resolve_profile(spec)
            # Trust the pipeline resolver for builtin specs (it is the
            # monolith's own path); keep ours when it found a DB row.
            if "niche_profiles" not in note and "hint" not in note:
                profile = candidate
        except Exception:
            pass
    ctx = make_context(
        spec, profile, get_base_dir(project, payload),
        project_id=_safe_project_id(project),
    )
    return spec, profile, ctx, note


def _resolved_topic(ctx: Any, project: dict) -> Optional[str]:
    """Topic resolved by an earlier research run, if checkpointed."""
    data = _read_json(Path(ctx.content_dir) / "research.json")
    if data:
        topic = data.get("resolved_topic") or data.get("topic")
        if isinstance(topic, str) and topic.strip() and not topic.startswith("auto:"):
            return topic.strip()
    raw = str(_get(project, "topic", default="") or "")
    if raw and not raw.startswith("auto:"):
        return raw
    return None


def _context_for(project: dict, payload: Optional[dict] = None):
    """Context whose spec topic is the research-resolved topic."""
    payload = payload or {}
    # First pass (topic unknown yet) to locate the work dir.
    spec0, profile0, ctx0, note0 = _resolve_context(project, payload)
    resolved = _resolved_topic(ctx0, project)
    if resolved and resolved != spec0.topic:
        spec, profile, ctx, note = _resolve_context(
            project, payload, topic_override=resolved
        )
        return spec, profile, ctx, note
    return spec0, profile0, ctx0, note0


def _record_state(ctx: Any, stage: str, summary: dict) -> None:
    state_path = Path(ctx.root) / "bridge_state.json"
    state = _read_json(state_path) or {}
    state[stage] = summary
    _write_json(state_path, state)


def _result(
    stage: str,
    project: dict,
    artefact: dict,
    *,
    file_path: Optional[str] = None,
    provenance: Optional[dict] = None,
    cost_usd: Optional[float] = None,
    cached: bool = False,
    extra: Optional[dict] = None,
) -> dict:
    artefact = dict(artefact)
    artefact.setdefault("stage", stage)
    if file_path:
        artefact.setdefault("file_path", file_path)
    out: dict[str, Any] = {
        "stage": stage,
        "project_id": str(_get(project, "id", "project_id", default="")),
        "artefact": artefact,
        "provenance": dict(provenance or {}),
    }
    if file_path:
        out["file_path"] = file_path
    # D1 fix: a zero cost means "no recorded cost" (e.g. a $0
    # deterministic bundle), NOT an authoritative $0 — emitting
    # ``cost_micros=0`` explicitly made handlers._resolve_cost treat
    # it as authoritative and suppress its per-stage cost table
    # (script: 25000).  Omit both cost fields when zero/unknown so
    # the handler falls back to STAGE_COST_MICROS; a non-zero
    # recorded cost still overrides the table exactly as before.
    if cost_usd is not None:
        try:
            cost_value = float(cost_usd)
        except (TypeError, ValueError):
            cost_value = 0.0
        if cost_value != 0.0:
            out["cost_usd"] = cost_value
            out["cost_micros"] = int(round(cost_value * 1_000_000))
    if cached:
        out["cached"] = True
        artefact["cached"] = True
    if extra:
        out.update(extra)
    return out


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------

_AUTO_TOPIC_TEMPLATES = [
    "{name} ke Hairan Kun Raaz jo Bohot Kam Log Jaante Hain",
    "{name} ki Woh Kahani jo Aap ko Hairan Kar Degi",
    "{name} ke Sab Se Bare Raaz — Poori Tafseel",
    "{name} ki Chhupi Hui Haqeeqat",
]


def _auto_topic(profile: Any, project: dict, sentinel: str) -> str:
    """Deterministic topic ideation for the ``auto:`` sentinel.

    The suffix after ``auto:`` is the niche_profile_id (a DB id, not a
    human topic), so ideation is driven by the resolved profile's
    name — seeded by the project id so different projects in one niche
    do not all collide on the same template.
    """
    name = str(getattr(profile, "name", "") or "Facts").strip()
    # Strip a trailing generic word duplication: profile names like
    # "Facts / Curiosity" read better by their head.
    head = name.split("/")[0].strip() or name
    seed_material = f"{_get(project, 'id', default='')}|{sentinel}|{head}"
    idx = int(hashlib.sha256(seed_material.encode()).hexdigest()[:8], 16)
    template = _AUTO_TOPIC_TEMPLATES[idx % len(_AUTO_TOPIC_TEMPLATES)]
    return template.format(name=head)


def _learning_conn_factory(project: dict, payload: dict):
    """Conn factory for the Learning Store reads in ``run_research``.

    An explicit ``db_path`` on the payload/project wins (the same
    resolution ``_try_db_rows`` uses); otherwise the app's configured
    DB (``app.db.get_connection``) — the database the execution
    service itself runs against.
    """
    from functools import partial

    for source in (payload, project):
        value = _get(source, "db_path", "database_path")
        if value:
            from app import db as _db

            return partial(_db.connect, str(value))
    from app import db as _db

    return _db.get_connection


def _learning_enrichment(
    project: dict,
    payload: dict,
    profile: Any,
    spec: Any,
    resolved: str,
    is_auto: bool,
) -> dict:
    """Learning consumption for one research run (Round 5 seam).

    INTEGRATION POINT: ``run_research`` below — the real DAG research
    stage.  The seam (:mod:`app.learning.consumption`) fetches the
    ACTIVE learning rules for the project's exact
    ``(channel_id, niche)`` scope and ranks the stage's candidate
    topics against them, returning structured DATA that is recorded
    verbatim on the research brief (``brief["learning"]`` +
    ``brief["applied_learning_rules"]``) so rule application is
    visible in the research report / ``research.json`` checkpoint.

    Bounded by design: the enrichment never writes or assembles a
    prompt, never injects rule text into any prompt layer, and never
    changes the resolved topic — it annotates, it does not steer.
    The niche scope is the niche profile's KEY (the Learning Store's
    scope spelling): the DB row's ``key`` when a row resolved, else
    the spec's niche id.  A failure of the advisory seam is recorded
    on the brief as ``status='unavailable'`` with the error — never
    silently dropped, and never fatal to research itself.
    """
    channel_id = str(_get(project, "channel_id", default="") or "").strip()
    niche_row, _channel_row = _try_db_rows(project, payload)
    niche_scope = ""
    if niche_row is not None and str(niche_row.get("key") or "").strip():
        niche_scope = str(niche_row["key"]).strip()
    if not niche_scope:
        niche_scope = str(
            getattr(spec, "niche", "") or getattr(profile, "profile_id", "") or ""
        ).strip()
    if not channel_id or not niche_scope:
        return {
            "status": "unavailable",
            "reason": "project carries no channel_id / niche scope",
            "channel_id": channel_id,
            "niche": niche_scope,
            "applied_rules": [],
            "suggestions": [],
        }

    candidates: list[str] = []
    raw_candidates = _get(payload, "candidate_topics", "candidates")
    if isinstance(raw_candidates, (list, tuple)):
        candidates = [str(c).strip() for c in raw_candidates if str(c).strip()]
    if not candidates and is_auto:
        name = str(getattr(profile, "name", "") or "Facts").strip()
        head = name.split("/")[0].strip() or name
        candidates = [t.format(name=head) for t in _AUTO_TOPIC_TEMPLATES]
    if resolved and resolved not in candidates:
        candidates.insert(0, resolved)
    if not candidates:
        candidates = [resolved]

    try:
        from app.learning.consumption import build_topic_suggestions

        result = build_topic_suggestions(
            channel_id,
            niche_scope,
            candidates,
            conn_factory=_learning_conn_factory(project, payload),
        )
        return {"status": "ok", **result}
    except Exception as exc:  # noqa: BLE001 — recorded, never silent
        return {
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
            "channel_id": channel_id,
            "niche": niche_scope,
            "applied_rules": [],
            "suggestions": [],
        }


def run_research(project: dict, payload: Optional[dict] = None) -> dict:
    """DAG ``research``: resolve the topic + build the research brief."""
    payload = payload or {}
    spec, profile, ctx, note = _resolve_context(project, payload)
    checkpoint = Path(ctx.content_dir) / "research.json"

    cached = _read_json(checkpoint)
    if cached and cached.get("resolved_topic"):
        return _result(
            "research", project, cached,
            file_path=str(checkpoint),
            provenance=cached.get("provenance") or {},
            cached=True,
        )

    raw_topic = str(_get(project, "topic", default="") or spec.topic or "")
    is_auto = raw_topic.strip().startswith("auto:")
    if is_auto:
        resolved = _auto_topic(profile, project, raw_topic.strip())
        topic_source = "auto-ideation-deterministic"
    else:
        resolved = raw_topic.strip() or spec.topic
        topic_source = "provided"

    # Research brief from the real content helpers (keyword extraction
    # + the profile's section plan / word budgets — the same planning
    # the script stage consumes).
    from app.integration import content as content_mod

    spec_resolved = spec.model_copy(update={"topic": resolved})
    target_words = int(round(profile.target_wpm * spec_resolved.minutes))
    plan = content_mod._plan_sections(profile, target_words)
    budgets = content_mod._budgets([role for _, role in plan], target_words)
    keywords = content_mod._keywords(resolved)
    brief = {
        "stage": "research",
        "topic": resolved,
        "resolved_topic": resolved,
        "original_topic": raw_topic,
        "topic_source": topic_source,
        "niche_id": str(getattr(profile, "profile_id", "")),
        "niche_name": str(getattr(profile, "name", "")),
        "profile_resolution": note,
        "keywords": keywords,
        "target_minutes": spec_resolved.minutes,
        "target_words": target_words,
        "outline": [
            {"title": title, "role": role, "word_budget": budget}
            for (title, role), budget in zip(plan, budgets)
        ],
        "structure_template": list(profile.structure_template),
        "hook_families": list(profile.hook_families),
        "language": spec_resolved.language,
        "summary": (
            f"Research brief for {resolved!r} in niche "
            f"{getattr(profile, 'name', '')!r}: {len(plan)} sections, "
            f"~{target_words} words target."
        ),
    }
    # Learning consumption seam (Round 5): active rules for this
    # project's (channel_id, niche) as DATA on the brief — visible in
    # the research report, never injected into any prompt, and the
    # resolved topic above is NOT changed by it.
    learning = _learning_enrichment(
        project, payload, profile, spec, resolved, is_auto
    )
    brief["learning"] = learning
    brief["applied_learning_rules"] = list(learning.get("applied_rules") or [])
    provenance = {
        "research_path": "content-helpers (keywords + section plan)",
        "topic_resolution": topic_source,
        "llm_path": "not-run (LLM runs at the script stage)",
        "learning_status": learning.get("status"),
        "learning_rules_applied": [
            r.get("rule_id") for r in brief["applied_learning_rules"]
        ],
    }
    brief["provenance"] = provenance
    _write_json(checkpoint, brief)
    _record_state(ctx, "research", {"topic": resolved, "file": str(checkpoint)})
    return _result(
        "research", project, brief,
        file_path=str(checkpoint), provenance=provenance,
    )


# ---------------------------------------------------------------------------
# script
# ---------------------------------------------------------------------------

def _build_content(spec: Any, profile: Any, registry_path: Any = None) -> Any:
    """Content bundle via the real content stage (loop-safe).

    ``registry_path`` (from ``payload["llm_registry_path"]``) is an
    explicit caller-supplied model-registry override — e.g. the
    Round-3 flash-lite tier substitution for keys whose T1/T2 models
    are quota-limited.  It is recorded in the script provenance; the
    bundle's own ``llm_models`` always shows what actually ran.
    """
    override = globals().get("build_content_auto")
    if callable(override):
        return override(spec, profile)
    kwargs: dict[str, Any] = {}
    if registry_path:
        kwargs["registry_path"] = registry_path
    # D2 fix: drive build_content_live through the run-loop owner —
    # and ONLY through it.  run_coroutine already handles every loop
    # context: the produce run's loop, no loop (one-call boundary
    # loop), and a foreign running loop (the execution handlers call
    # us from async code — it bridges via a worker thread).  The
    # previous ``except: build_content_auto(...)`` fallback was the
    # defect: when the live call raised (e.g. a Gemini 429), the
    # fallback ran in the handler's thread — inside its active event
    # loop — and build_content_auto raises RuntimeError("cannot run
    # inside an active event loop") by design, masking the real
    # error; the handler then silently stubbed the stage.  Now a
    # live failure propagates verbatim to the handler (which logs it
    # before falling back), and the deterministic bundle remains
    # reachable ONLY the honest way: build_content_live itself
    # returns it when no Gemini key resolves.
    from app.integration.content import build_content_live
    from app.integration.run_loop import run_coroutine

    return run_coroutine(lambda: build_content_live(spec, profile, **kwargs))


def _load_bundle(ctx: Any) -> Optional[Any]:
    data = _read_json(Path(ctx.content_dir) / "bundle.json")
    if not data:
        return None
    try:
        from app.integration.content import ContentBundle

        return ContentBundle(**{k: v for k, v in data.items()
                                if k in ContentBundle.model_fields})
    except Exception:
        return None


def _ensure_bundle(project: dict, payload: dict, ctx: Any) -> Any:
    bundle = _load_bundle(ctx)
    if bundle is not None:
        return bundle
    result = run_script(project, payload)
    bundle = _load_bundle(ctx)
    if bundle is None:  # script stage returned a dict-shaped bundle
        artefact = result.get("artefact") or {}
        from app.integration.content import ContentBundle

        bundle = ContentBundle(**{k: v for k, v in artefact.items()
                                  if k in ContentBundle.model_fields})
    return bundle


def run_script(project: dict, payload: Optional[dict] = None) -> dict:
    """DAG ``script``: real content generation (Gemini or stub-marked)."""
    payload = payload or {}
    spec, profile, ctx, note = _context_for(project, payload)
    checkpoint = Path(ctx.content_dir) / "bundle.json"

    cached_bundle = _load_bundle(ctx)
    if cached_bundle is not None:
        data = cached_bundle.model_dump(mode="json")
        provenance = {
            "llm_path": cached_bundle.llm_path,
            "llm_models": list(cached_bundle.llm_models),
            "profile_resolution": note,
        }
        return _result(
            "script", project, data,
            file_path=str(checkpoint), provenance=provenance,
            cost_usd=float(cached_bundle.llm_cost_usd or 0.0), cached=True,
        )

    # Auto topics must be resolved by research before scripting.
    if str(spec.topic or "").startswith("auto:"):
        run_research(project, payload)
        spec, profile, ctx, note = _context_for(project, payload)

    registry_path = _get(payload, "llm_registry_path", "registry_path")
    bundle = _build_content(spec, profile, registry_path=registry_path)
    data = _to_dict(bundle)
    _write_json(checkpoint, data)
    provenance = {
        "llm_path": str(_get(bundle, "llm_path", default="deterministic-stub")),
        "llm_models": list(_get(bundle, "llm_models", default=[]) or []),
        "profile_resolution": note,
    }
    if registry_path:
        provenance["llm_registry_path"] = str(registry_path)
    _record_state(ctx, "script", {
        "llm_path": provenance["llm_path"],
        "title": str(_get(bundle, "title", default="")),
        "file": str(checkpoint),
    })
    return _result(
        "script", project, data,
        file_path=str(checkpoint), provenance=provenance,
        cost_usd=float(_get(bundle, "llm_cost_usd", default=0.0) or 0.0),
    )


# ---------------------------------------------------------------------------
# voice
# ---------------------------------------------------------------------------

def _load_voice(ctx: Any) -> Optional[Any]:
    data = _read_json(Path(ctx.audio_dir) / "voice.json")
    if not data:
        return None
    try:
        from app.integration.voice_stage import VoiceResult

        return VoiceResult(**{k: v for k, v in data.items()
                              if k in VoiceResult.model_fields})
    except Exception:
        return SimpleNamespace(**data)


def run_voice(project: dict, payload: Optional[dict] = None) -> dict:
    """DAG ``voice``: real edge-tts synthesis (voice stage cache inside)."""
    payload = payload or {}
    spec, profile, ctx, note = _context_for(project, payload)
    checkpoint = Path(ctx.audio_dir) / "voice.json"

    cached = _load_voice(ctx)
    if cached is not None and Path(str(_get(cached, "narration_wav", default=""))).is_file():
        data = _to_dict(cached)
        provenance = {"tts": str(_get(cached, "path_used", default="")),
                      "voice_id": str(_get(cached, "voice_id", default=""))}
        return _result(
            "voice", project, data,
            file_path=str(_get(cached, "narration_wav", default="")),
            provenance=provenance, cached=True,
        )

    bundle = _ensure_bundle(project, payload, ctx)
    voice = _stage_fn("synthesize_voice")(bundle, profile, ctx)
    data = _to_dict(voice)
    _write_json(checkpoint, data)
    provenance = {
        "tts": str(_get(voice, "path_used", default="")),
        "voice_id": str(_get(voice, "voice_id", default="")),
    }
    _record_state(ctx, "voice", {"tts": provenance["tts"], "file": str(checkpoint)})
    return _result(
        "voice", project, data,
        file_path=str(_get(voice, "narration_wav", default="")) or None,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# visuals
# ---------------------------------------------------------------------------

def _load_visuals(ctx: Any) -> Optional[Any]:
    data = _read_json(Path(ctx.visuals_dir) / "visuals.json")
    if not data:
        return None
    try:
        from app.integration.visuals_stage import VisualsResult

        fields = {k: v for k, v in data.items()
                  if k in VisualsResult.__dataclass_fields__}  # type: ignore[attr-defined]
        return VisualsResult(**fields)
    except Exception:
        return SimpleNamespace(**data)


def run_visuals(project: dict, payload: Optional[dict] = None) -> dict:
    """DAG ``visuals``: stock-first scene visuals (plates on fallback)."""
    payload = payload or {}
    spec, profile, ctx, note = _context_for(project, payload)
    checkpoint = Path(ctx.visuals_dir) / "visuals.json"

    cached = _load_visuals(ctx)
    if cached is not None and _get(cached, "scene_manifest"):
        data = _to_dict(cached)
        sources = list(_get(cached, "scene_sources", default=[]) or [])
        provenance = {
            "visuals": str(_get(cached, "path_used", default="")
                           or ("stock-first" if _get(cached, "stock_count") else "local-plates")),
            "scene_sources": sources,
            "stock_share": _get(cached, "stock_share", default=0.0),
        }
        return _result(
            "visuals", project, data,
            file_path=str(_get(cached, "manifest_path", default="")) or None,
            provenance=provenance, cached=True,
        )

    bundle = _ensure_bundle(project, payload, ctx)
    voice = _load_voice(ctx)
    if voice is None:
        run_voice(project, payload)
        voice = _load_voice(ctx)
    visuals = _stage_fn("plan_and_generate_visuals")(bundle, voice, profile, ctx)
    data = _to_dict(visuals)
    _write_json(checkpoint, data)
    sources = list(_get(visuals, "scene_sources", default=[]) or [])
    stock_count = int(_get(visuals, "stock_count", default=0) or 0)
    provenance = {
        "visuals": "stock-first" if stock_count else "local-plates",
        "scene_sources": sources,
        "stock_share": _get(visuals, "stock_share", default=0.0),
        "stock_count": stock_count,
        "plate_count": int(_get(visuals, "plate_count", default=0) or 0),
    }
    _record_state(ctx, "visuals", {"visuals": provenance["visuals"],
                                   "file": str(checkpoint)})
    return _result(
        "visuals", project, data,
        file_path=str(_get(visuals, "manifest_path", default="")) or None,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# render / thumbnail / qc — one shared assemble run
# ---------------------------------------------------------------------------

def _load_assemble(ctx: Any) -> Optional[Any]:
    data = _read_json(Path(ctx.render_dir) / "assemble.json")
    if not data:
        return None
    return SimpleNamespace(**data)


def _call_assemble_project(fn: Any, bundle: Any, voice: Any, visuals: Any,
                           profile: Any, ctx: Any) -> Any:
    """Call the assemble stage with the spec's ``quality_tier``.

    Round 5 seam: the tier resolved onto ``ctx.spec`` (per-channel,
    see :func:`project_to_spec`) is passed as the assemble/render
    entry point's ``quality_tier`` argument — but only when the
    resolved callable actually accepts that parameter (the real
    ``assemble_stage.assemble_project`` does; contract-shaped test
    fakes that predate the parameter keep being called with the
    historical five positional arguments).
    """
    tier = _get(_get(ctx, "spec"), "quality_tier") or _get(ctx, "quality_tier")
    if tier:
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        accepts = "quality_tier" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if accepts:
            return fn(bundle, voice, visuals, profile, ctx,
                      quality_tier=str(tier))
    return fn(bundle, voice, visuals, profile, ctx)


def _ensure_assemble(project: dict, payload: dict, ctx: Any, profile: Any) -> Any:
    cached = _load_assemble(ctx)
    if cached is not None and Path(str(_get(cached, "final_mp4", default=""))).is_file():
        return cached

    bundle = _ensure_bundle(project, payload, ctx)
    voice = _load_voice(ctx)
    if voice is None:
        run_voice(project, payload)
        voice = _load_voice(ctx)
    visuals = _load_visuals(ctx)
    if visuals is None:
        run_visuals(project, payload)
        visuals = _load_visuals(ctx)

    assemble = _call_assemble_project(
        _stage_fn("assemble_project"), bundle, voice, visuals, profile, ctx
    )
    data = _to_dict(assemble)
    # qc_report is a live object, not JSON — the summary is the record.
    data.pop("qc_report", None)
    # assemble returns the SRT *text* in captions_srt; the file lives
    # at <pack_dir>/captions.srt (same normalisation as produce.py).
    raw_captions = data.get("captions_srt")
    srt_file = Path(ctx.pack_dir) / "captions.srt"
    if srt_file.is_file():
        data["captions_path"] = str(srt_file)
        if isinstance(raw_captions, str) and "-->" in raw_captions:
            data["captions_srt_text_chars"] = len(raw_captions)
            data["captions_srt"] = str(srt_file)
    checkpoint = Path(ctx.render_dir) / "assemble.json"
    _write_json(checkpoint, data)
    qc_path = Path(ctx.render_dir) / "qc.json"
    _write_json(qc_path, {
        "qc_passed": bool(_get(assemble, "qc_passed", default=False)),
        "qc_summary": _get(assemble, "qc_summary", default={}),
        "loudness_lufs": _get(assemble, "loudness_lufs"),
        "duration_s": _get(assemble, "duration_s"),
        "final_mp4": str(_get(assemble, "final_mp4", default="")),
    })
    _record_state(ctx, "assemble", {"final_mp4": data.get("final_mp4"),
                                    "file": str(checkpoint)})
    return SimpleNamespace(**data)


def _assemble_provenance(assemble: Any) -> dict:
    subtitle = str(_get(assemble, "subtitle_path_used", default="") or "")
    return {
        "render": str(_get(assemble, "render_path_used", default="draft")),
        "quality_tier": str(
            _get(assemble, "quality_tier", default="") or "draft"),
        "subtitles": subtitle or str(_get(assemble, "subtitle_source", default="")),
        "subtitle_source": str(_get(assemble, "subtitle_source", default="")),
    }


def run_render(project: dict, payload: Optional[dict] = None) -> dict:
    """DAG ``render``: assemble the master video (render + subtitles)."""
    payload = payload or {}
    spec, profile, ctx, note = _context_for(project, payload)
    was_cached = _load_assemble(ctx) is not None
    assemble = _ensure_assemble(project, payload, ctx, profile)
    data = _to_dict(assemble)
    return _result(
        "render", project, data,
        file_path=str(_get(assemble, "final_mp4", default="")) or None,
        provenance=_assemble_provenance(assemble), cached=was_cached,
    )


def run_thumbnail(project: dict, payload: Optional[dict] = None) -> dict:
    """DAG ``thumbnail``: the assemble run's Pillow thumbnail variants."""
    payload = payload or {}
    spec, profile, ctx, note = _context_for(project, payload)
    was_cached = _load_assemble(ctx) is not None
    assemble = _ensure_assemble(project, payload, ctx, profile)
    thumbnails = list(_get(assemble, "thumbnails", default=[]) or [])
    artefact = {
        "stage": "thumbnail",
        "thumbnails": thumbnails,
        "thumbnail_count": len(thumbnails),
        "final_mp4": str(_get(assemble, "final_mp4", default="")),
    }
    return _result(
        "thumbnail", project, artefact,
        file_path=thumbnails[0] if thumbnails else None,
        provenance={"thumbnails": "pillow-variants (assemble stage)"},
        cached=was_cached,
    )


def run_qc(project: dict, payload: Optional[dict] = None) -> dict:
    """DAG ``qc``: the assemble run's technical-QC verdict, verbatim."""
    payload = payload or {}
    spec, profile, ctx, note = _context_for(project, payload)
    was_cached = _load_assemble(ctx) is not None
    assemble = _ensure_assemble(project, payload, ctx, profile)
    qc_path = Path(ctx.render_dir) / "qc.json"
    if not qc_path.is_file():
        _write_json(qc_path, {
            "qc_passed": bool(_get(assemble, "qc_passed", default=False)),
            "qc_summary": _get(assemble, "qc_summary", default={}),
            "loudness_lufs": _get(assemble, "loudness_lufs"),
            "duration_s": _get(assemble, "duration_s"),
        })
    artefact = {
        "stage": "qc",
        "qc_passed": bool(_get(assemble, "qc_passed", default=False)),
        "qc_summary": _get(assemble, "qc_summary", default={}),
        "loudness_lufs": _get(assemble, "loudness_lufs"),
        "duration_s": _get(assemble, "duration_s"),
        "final_mp4": str(_get(assemble, "final_mp4", default="")),
    }
    provenance = _assemble_provenance(assemble)
    provenance["qc"] = "technical-qc (assemble stage)"
    return _result(
        "qc", project, artefact,
        file_path=str(qc_path), provenance=provenance, cached=was_cached,
    )


# ---------------------------------------------------------------------------
# package
# ---------------------------------------------------------------------------

def run_package(project: dict, payload: Optional[dict] = None) -> dict:
    """DAG ``package``: Gate B + frozen manifest (fake provider) + pack."""
    payload = payload or {}
    spec, profile, ctx, note = _context_for(project, payload)
    checkpoint = Path(ctx.pack_dir) / "package.json"

    cached = _read_json(checkpoint)
    if cached and cached.get("manifest_path") and Path(str(cached["manifest_path"])).is_file():
        return _result(
            "package", project, cached,
            file_path=str(cached["manifest_path"]),
            provenance=cached.get("provenance") or {},
            cached=True,
        )

    bundle = _ensure_bundle(project, payload, ctx)
    assemble = _ensure_assemble(project, payload, ctx, profile)

    # Gate A verdict for the publish stage (real gate machinery).
    gate_a_passed, gate_a_reasons = False, []
    try:
        outcome = _stage_fn("run_gate_a")(bundle, profile)
        if isinstance(outcome, (tuple, list)) and outcome:
            gate_a_passed = bool(outcome[0])
            gate_a_reasons = [str(r) for r in (outcome[1] if len(outcome) > 1 else [])]
        elif isinstance(outcome, bool):
            gate_a_passed = outcome
    except Exception as exc:
        gate_a_reasons = [f"gate A evaluation failed: {type(exc).__name__}"]

    # Licence rows for the copyright gates, with produce.py's one
    # vocabulary translation (local_plate -> generated).
    qc_rows: list[dict] = []
    for seg in _get(assemble, "licence_segments", default=[]) or []:
        row = _to_dict(seg)
        if str(row.get("provider") or "").strip().lower() in {
            "local_plate", "local-plates", "local plate", "localplate",
        }:
            row["provider"] = "generated"
        qc_rows.append(row)
    captions_path = str(
        _get(assemble, "captions_path", default="")
        or (Path(ctx.pack_dir) / "captions.srt")
    )
    qc_info = {
        "qc_passed": bool(_get(assemble, "qc_passed", default=False)),
        "qc_summary": _get(assemble, "qc_summary", default={}),
        "loudness_lufs": _get(assemble, "loudness_lufs"),
        "duration_s": _get(assemble, "duration_s"),
        "final_mp4": str(_get(assemble, "final_mp4", default="")),
        "captions_srt": captions_path,
        "thumbnails": list(_get(assemble, "thumbnails", default=[]) or []),
        "title": str(_get(bundle, "title", default="")),
        "licence_segments": qc_rows,
    }
    # Round 6 closure (W7): evaluate Gate B under the project's own
    # publish mode, so the package report's verdict label is the one
    # the publish finaliser will apply for this project (a conditional
    # in an auto project is labelled blocked-auto here, not pending).
    # No approval is passed: at package time the Gate B approval does
    # not exist yet (the gate.b job runs AFTER package), so a
    # conditional is honestly labelled conditional_blocked /
    # approval-required — the finaliser supplies the recorded
    # approval when the gate decision lands.  Signature-aware: test
    # fakes for finalize_publish may not accept the ``mode`` kwarg.
    _finalize = _stage_fn("finalize_publish")
    try:
        _finalize_params = inspect.signature(_finalize).parameters
    except (TypeError, ValueError):
        _finalize_params = {}
    if "mode" in _finalize_params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for p in _finalize_params.values()
    ):
        publish = _finalize(
            assemble, qc_info, bundle, profile, ctx, gate_a_passed,
            mode=str(spec.publish_mode or "review_first"),
        )
    else:
        publish = _finalize(
            assemble, qc_info, bundle, profile, ctx, gate_a_passed
        )

    # Shared Gate B verdict labelling (W7): carry the finalize
    # result's canonical verdict + typed reason codes into every
    # package surface below (report_data / artefact / provenance).
    # A bare ``gate_b_passed`` bool is never the only Gate B signal
    # this stage emits — a conditional release reads
    # ``conditional_pass`` and a block keeps e.g.
    # ``G2_PROVENANCE_FAILED`` attached.
    _gb_passed = bool(_get(publish, "gate_b_passed", default=False))
    _gb_outcome = str(_get(publish, "gate_b_outcome", default="") or "")
    if not _gb_outcome:
        _gb_outcome = "pass" if _gb_passed else "block"
    _gb_verdict = str(_get(publish, "gate_b_verdict", default="") or "")
    if not _gb_verdict:
        if _gb_passed and _gb_outcome == "conditional":
            _gb_verdict = "conditional_pass"
        elif _gb_passed:
            _gb_verdict = "pass"
        elif _gb_outcome == "conditional":
            _gb_verdict = "conditional_blocked"
        else:
            _gb_verdict = "block"
    _gb_codes = [str(c) for c in (
        _get(publish, "gate_b_reason_codes", default=[]) or [])]
    if not _gb_codes:
        for _p in (_get(publish, "gate_b_problems", default=[]) or []):
            _code = _p.get("code") if isinstance(_p, dict) else getattr(
                _p, "code", None)
            if _code and str(_code) not in _gb_codes:
                _gb_codes.append(str(_code))
    gate_b_fields = {
        "gate_b_verdict": _gb_verdict,
        "gate_b_outcome": _gb_outcome,
        "gate_b_reason_codes": _gb_codes,
    }
    manifest_path = str(
        _get(publish, "manifest_path", default="")
        or _get(publish, "publish_manifest_json", default="")
        or (Path(ctx.pack_dir) / "publish_manifest.json")
    )

    # Complete the pack: licence ledger CSV + report.md (pack writers),
    # mirroring produce.py's always-write-the-pack rule.
    ledger_csv = Path(ctx.pack_dir) / "licence_ledger.csv"
    try:
        from app.integration.ledger import LicenceLedger
        from app.integration.produce import _collect_ledger_rows, _ledger_add

        voice = _load_voice(ctx)
        visuals = _load_visuals(ctx)
        ledger = LicenceLedger()
        seen: set[tuple[str, str]] = set()
        for row in _collect_ledger_rows(voice, visuals, assemble, publish):
            rd = _to_dict(row)
            key = (str(rd.get("asset_id") or rd.get("asset") or ""),
                   str(rd.get("scene_id") or ""))
            if key != ("", "") and key in seen:
                continue
            if _ledger_add(ledger, row) and key != ("", ""):
                seen.add(key)
        ledger.write_csv(ledger_csv)
    except Exception:
        if not ledger_csv.is_file():
            ledger_csv.write_text(
                "asset_id,scene_id,source,licence,kind,local_path,notes\n",
                encoding="utf-8",
            )

    bundle_data = _to_dict(bundle)

    # -- Report integrity (Round 4, #5): derive, never recall --------
    # Same rule as produce.py's _finish (app.integration.report_facts):
    # this bridge's report.md fields come from the objects of record —
    # the LLM usage ledger built from the bundle's ledger data (also
    # persisted as llm_usage.json), the licence ledger CSV just
    # written (re-read from disk) unioned with the scene manifest, and
    # the frozen publish manifest — never from the bundle's declared
    # llm_path label or stage counters alone.  Best-effort: any
    # failure leaves the stage-value report_data below untouched.
    llm_payload: dict = {}
    visual_facts: dict = {}
    manifest_facts: dict = {}
    try:
        import csv as _csv

        from app.integration.report_facts import (
            build_llm_usage_ledger,
            facts_from_publish_manifest,
            facts_from_visual_records,
        )

        llm_payload = build_llm_usage_ledger(bundle)
        (Path(ctx.pack_dir) / "llm_usage.json").write_text(
            json.dumps(llm_payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        _vis = _load_visuals(ctx)
        _scene_records = (
            _get(_vis, "scene_manifest", default=[]) or []
        ) if _vis is not None else []
        _rows: list = []
        if ledger_csv.is_file():
            with ledger_csv.open(newline="", encoding="utf-8") as _fh:
                _rows = list(_csv.DictReader(_fh))
        visual_facts = facts_from_visual_records(_rows, _scene_records)
        _mp = Path(manifest_path)
        if _mp.is_file():
            manifest_facts = facts_from_publish_manifest(
                json.loads(_mp.read_text(encoding="utf-8"))
            )
    except Exception:
        llm_payload, visual_facts, manifest_facts = {}, {}, {}

    report_data = {
        "project_id": str(ctx.project_id),
        "niche_id": str(ctx.niche_id),
        "topic": str(spec.topic),
        "title": str(_get(bundle, "title", default=spec.topic)),
        "pack_dir": str(ctx.pack_dir),
        "final_mp4": str(_get(assemble, "final_mp4", default="")),
        "thumbnails": list(_get(assemble, "thumbnails", default=[]) or []),
        "captions_srt": captions_path,
        "licence_ledger_csv": str(ledger_csv),
        "publish_manifest_json": manifest_path,
        "duration_s": _get(assemble, "duration_s"),
        "loudness_lufs": _get(assemble, "loudness_lufs"),
        "cost_usd": float(_get(bundle, "llm_cost_usd", default=0.0) or 0.0),
        "paths_used": {
            "llm": str(_get(bundle, "llm_path", default="deterministic-stub")),
            "publish": "fake",
        },
        "gate_a_passed": gate_a_passed,
        "gate_b_passed": bool(_get(publish, "gate_b_passed", default=False)),
        **gate_b_fields,
        "qc_passed": bool(_get(assemble, "qc_passed", default=False)),
        "publish_state": str(_get(publish, "publish_state", default="")),
        "warnings": [f"Gate A: {r}" for r in gate_a_reasons] + (
            [
                f"Gate B: {_gb_verdict} "
                f"({', '.join(_gb_codes) or 'no reason codes'}) — "
                "conditional Gate B requires a recorded human approval "
                "in review_first mode and never publishes in auto mode"
            ]
            if _gb_verdict.startswith("conditional") else []
        ),
    }
    if llm_payload:
        report_data["llm_models"] = llm_payload.get("models") or []
        report_data["llm_usage"] = llm_payload.get("usage") or {}
        report_data["cost_usd"] = float(llm_payload.get("cost_usd") or 0.0)
        report_data["paths_used"]["llm"] = str(
            llm_payload.get("path") or report_data["paths_used"]["llm"])
        report_data["llm_usage_json"] = str(Path(ctx.pack_dir) / "llm_usage.json")
    if visual_facts.get("visual_sources"):
        report_data["visual_sources"] = visual_facts["visual_sources"]
        report_data["stock_count"] = visual_facts["stock_count"]
        report_data["plate_count"] = visual_facts["plate_count"]
        report_data["stock_share"] = visual_facts["stock_share"]
    for _key, _value in manifest_facts.items():
        report_data[_key] = _value
    report_path = Path(ctx.pack_dir) / "report.md"
    try:
        from app.integration.pack import write_report_md

        write_report_md(report_data, report_path)
    except Exception:
        pass

    provenance = {
        "publish": "fake",
        "publish_provider": "fake",
        "publish_state": str(_get(publish, "publish_state", default="")),
        "llm_path": str(_get(bundle, "llm_path", default="deterministic-stub")),
        "gate_a_passed": gate_a_passed,
        "gate_b_passed": bool(_get(publish, "gate_b_passed", default=False)),
        **gate_b_fields,
    }
    artefact = {
        "stage": "package",
        "title": str(_get(bundle, "title", default="")),
        "description": str(_get(bundle, "description", default="")),
        "tags": list(_get(bundle, "tags", default=[]) or []),
        "thumbnail_text": str(_get(bundle, "thumbnail_text", default="")),
        "manifest_path": manifest_path,
        "manifest": _get(publish, "manifest", default={}),
        "publish_state": str(_get(publish, "publish_state", default="")),
        "gate_a_passed": gate_a_passed,
        "gate_a_reasons": gate_a_reasons,
        "gate_b_passed": bool(_get(publish, "gate_b_passed", default=False)),
        **gate_b_fields,
        "final_mp4": str(_get(assemble, "final_mp4", default="")),
        "thumbnails": list(_get(assemble, "thumbnails", default=[]) or []),
        "captions_srt": captions_path,
        "licence_ledger_csv": str(ledger_csv),
        "report_md": str(report_path),
        "provenance": provenance,
    }
    _write_json(checkpoint, artefact)
    _record_state(ctx, "package", {"manifest": manifest_path,
                                   "file": str(checkpoint)})
    return _result(
        "package", project, artefact,
        file_path=manifest_path, provenance=provenance,
        cost_usd=float(_get(bundle, "llm_cost_usd", default=0.0) or 0.0),
    )
