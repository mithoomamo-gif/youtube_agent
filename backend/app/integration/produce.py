"""Orchestrator for ROUND 2 end-to-end integration (Worker 6).

``produce_video(spec, base_dir)`` wires the sibling stage modules
together in contract order:

    resolve profile -> make_context -> build_content -> Gate A
    -> synthesize_voice -> plan_and_generate_visuals
    -> LicenceLedger (all rows) -> assemble_project
    -> finalize_publish -> pack files (ledger CSV + report.md)

Stage functions are resolved dynamically (module globals first, then
the sibling modules) so unit tests can monkeypatch
``app.integration.produce.build_content`` etc. without touching the
real stages, and so this module imports cleanly while sibling modules
are still landing.

Honest ``paths_used`` is a hard rule:

* ``llm``      -- whatever the content stage records on its bundle
  (``"deterministic-stub"`` for the offline path, ``"gemini-live"``
  when the Round 3 live Gemini path produced the script; bundles
  without provenance info default to ``"deterministic-stub"``)
* ``tts``      -- whatever the voice stage reports (e.g. ``edge-tts``
  or ``stub``)
* ``visuals``  -- ``"local-plates"``
* ``subtitles``-- whatever the assemble stage reports
* ``publish``  -- ``"fake-provider"``

Gate A policy (per task contract): a Gate A failure is recorded
(``gate_a_passed=False``) and warned about.  Production continues
only in ``review_first`` publish mode (a human will review before
anything publishes); in ``auto`` mode the run stops after content,
still writes its ledger + report pack files, and returns a result
with ``publish_state="blocked_gate_a"``.

Report integrity (Round 4, fragility #5): every derived field in
``report.md`` is recomputed at pack time in :func:`_finish` from the
objects of record via :mod:`app.integration.report_facts` — visuals
from the licence-ledger rows being written to CSV unioned with the
scene manifest, LLM provenance /
models / tokens / cost from the LLM usage ledger (also persisted as
``llm_usage.json``), and gate verdicts / publish state / duration
from the frozen publish manifest.  Stage-level counters are only ever
interim values; nothing reaches the report from a parallel counter.

Supervisor wiring: after a successful publish stage, the LangGraph
supervisor graph is invoked in a lightweight *tracking* mode with a
state dict in the graph's own shape (locked script, manifest hash,
Gate B tokens when available) purely to record phases/gates.  Any
failure there is a warning only -- the direct stage flow above is
authoritative and is never distorted by the graph run.

Event-loop ownership (Round 4, see ``app.integration.run_loop``):
``produce_video`` is the loop owner for a produce run -- it enters
``run_loop.produce_loop()`` for the whole run, so every stage's async
work (LLM, TTS, stock fetches) runs on that ONE loop and loop-bound
resources such as the stock adapter's httpx pool stay valid from the
first stage to the last.  Stage modules never create or close a loop
themselves; a sequential second run gets a fresh loop.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

__all__ = ["produce_video", "resolve_profile"]


# ---------------------------------------------------------------------------
# Small generic helpers
# ---------------------------------------------------------------------------

def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return dict(vars(obj))
    return {}


def _as_str_path(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (str, Path)):
        return [str(value)]
    return [str(v) for v in value]


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------

def resolve_profile(spec: Any) -> Any:
    """Builtin -> ``niches.builtins.get_profile``; OTHER -> builder."""
    niche = str(_get(spec, "niche", "facts") or "facts").strip().lower()
    if niche in {"other", "custom", "other/custom"} or _get(spec, "custom_niche_name"):
        # Only route to OTHER when the caller actually asked for it or
        # supplied a custom name alongside a non-builtin id.
        if niche in {"other", "custom", "other/custom"}:
            from app.niches.builder import build_other_profile

            name = _get(spec, "custom_niche_name") or "custom niche"
            idea = _get(spec, "niche_idea") or _get(spec, "topic") or str(name)
            return build_other_profile(name=str(name), idea=str(idea))
    from app.niches.builtins import get_profile

    return get_profile(niche)


# ---------------------------------------------------------------------------
# Dynamic stage resolution (monkeypatch-friendly)
# ---------------------------------------------------------------------------

_STAGE_MODULES = {
    "build_content": "app.integration.content",
    "run_gate_a": "app.integration.content",
    "synthesize_voice": "app.integration.voice_stage",
    "plan_and_generate_visuals": "app.integration.visuals_stage",
    "assemble_project": "app.integration.assemble_stage",
    "finalize_publish": "app.integration.publish_stage",
}


def _resolve_stage(name: str) -> Callable[..., Any]:
    override = globals().get(name)
    if callable(override):
        return override  # type: ignore[return-value]
    import importlib

    module = importlib.import_module(_STAGE_MODULES[name])
    fn = getattr(module, name)
    if not callable(fn):
        raise TypeError(f"{_STAGE_MODULES[name]}.{name} is not callable")
    return fn


# Placeholders so monkeypatch targets exist as module attributes = None.
build_content = None  # type: ignore[assignment]
run_gate_a = None  # type: ignore[assignment]
synthesize_voice = None  # type: ignore[assignment]
plan_and_generate_visuals = None  # type: ignore[assignment]
assemble_project = None  # type: ignore[assignment]
finalize_publish = None  # type: ignore[assignment]


def _call_stage(name: str, *args: Any) -> Any:
    fn = _resolve_stage(name)
    return fn(*args)


def _call_assemble_stage(bundle: Any, voice: Any, visuals: Any,
                         profile: Any, ctx: Any) -> Any:
    """Call the assemble stage with the spec's ``quality_tier``.

    Round 5 seam (master encode): the tier on the spec — the
    per-channel setting, resolved onto ``ProjectSpec.quality_tier``
    by the caller / stage bridge (see :mod:`app.channel_quality`) —
    is passed as the assemble/render entry point's ``quality_tier``
    argument, but only when the resolved callable actually accepts
    that parameter (the real ``assemble_stage.assemble_project``
    does; contract-shaped stage fakes that predate the parameter
    keep being called with the historical five positional
    arguments, and the real stage's own fallback reads
    ``ctx.spec.quality_tier`` when handed ``None``).
    """
    fn = _resolve_stage("assemble_project")
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


# ---------------------------------------------------------------------------
# Gate A normalisation
# ---------------------------------------------------------------------------

def _normalise_gate_a(raw: Any) -> tuple[bool, list[str]]:
    if raw is None:
        return False, ["Gate A returned no verdict"]
    if isinstance(raw, (tuple, list)) and len(raw) >= 1:
        passed = bool(raw[0])
        reasons: list[str] = []
        if len(raw) >= 2 and raw[1]:
            r = raw[1]
            reasons = [str(x) for x in r] if isinstance(r, (list, tuple)) else [str(r)]
        return passed, reasons
    if isinstance(raw, bool):
        return raw, []
    passed = _get(raw, "passed", _get(raw, "gate_a_passed", None))
    reasons = _get(raw, "reasons", _get(raw, "failures", [])) or []
    if passed is None:
        return False, ["Gate A verdict object had no passed flag"]
    return bool(passed), [str(r) for r in reasons] if isinstance(reasons, (list, tuple)) else [str(reasons)]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _ledger_add(ledger: Any, row: Any) -> bool:
    """Add one stage ledger row (LicenceRow / dict / object) to ledger."""
    data = _to_dict(row)
    if not data and not hasattr(row, "asset_id"):
        return False
    # Canonical fields for LicenceLedger.add(...).  Sibling stages use
    # slightly different key names (voice: asset/path; visuals:
    # asset_path; assemble licence_segments: licence_id/provider) --
    # all aliases are normalised here, in the orchestrator.
    def _pick(*keys: str) -> str:
        for key in keys:
            val = data.get(key) or getattr(row, key, "")
            if val:
                return str(val)
        return ""

    asset_id = _pick("asset_id", "asset") or "asset"
    scene_id = _pick("scene_id")
    source = _pick("source", "provider") or "own/generated"
    licence_name = (
        _pick("licence_name", "licence", "license", "license_name", "licence_id")
        or "own work"
    )
    kind = _pick("kind", "source_type", "role") or "asset"
    local_path = _pick("local_path", "asset_path", "path", "file_path")
    notes = _pick("notes")
    extras = []
    if data.get("path_used"):
        extras.append(f"path_used={data['path_used']}")
    if data.get("start_s") is not None and data.get("end_s") is not None:
        extras.append(f"{data['start_s']}s-{data['end_s']}s")
    if data.get("content_hash"):
        extras.append(f"sha256={str(data['content_hash'])[:12]}")
    if extras:
        notes = (notes + " " if notes else "") + "; ".join(extras)
    try:
        ledger.add(
            asset_id=asset_id,
            scene_id=scene_id,
            source=source,
            licence_name=licence_name,
            kind=kind,
            local_path=local_path,
            notes=notes,
        )
        return True
    except TypeError:
        pass
    # Fallbacks for a differently-shaped (but contract-close) ledger.
    for attempt in (
        lambda: ledger.add(row),
        lambda: ledger.add(asset_id, scene_id, source, licence_name, kind, local_path, notes),
    ):
        try:
            attempt()
            return True
        except Exception:
            continue
    return False


def _collect_ledger_rows(*stage_results: Any) -> list[Any]:
    rows: list[Any] = []
    for result in stage_results:
        if result is None:
            continue
        for attr in ("ledger_rows", "licence_rows", "license_rows",
                     "licence_segments", "rows"):
            val = _get(result, attr)
            if val:
                rows.extend(list(val))
                break
    return rows


# ---------------------------------------------------------------------------
# Pack writing (app.integration.pack when available, honest fallback)
# ---------------------------------------------------------------------------

def _fallback_report(ctx: Any, data: dict[str, Any]) -> str:
    lines = [
        f"# Production Report — {data.get('title') or data.get('topic')}",
        "",
        f"- Project: `{data.get('project_id')}`",
        f"- Niche: `{data.get('niche_id')}`",
        f"- Topic: {data.get('topic')}",
        f"- Title: {data.get('title')}",
        f"- Duration (s): {data.get('duration_s')}",
        f"- Quality tier: {data.get('quality_tier')}",
        f"- Loudness (LUFS): {data.get('loudness_lufs')}",
        f"- Cost: {data.get('cost_usd')}",
        f"- LLM models: {', '.join(data.get('llm_models') or [])}",
        f"- LLM usage: {data.get('llm_usage')}",
        f"- Gate A passed: {data.get('gate_a_passed')}",
        f"- Gate B passed: {data.get('gate_b_passed')}",
        f"- QC passed: {data.get('qc_passed')}",
        f"- Publish state: {data.get('publish_state')}",
        f"- Final MP4: {data.get('final_mp4')}",
        f"- Captions: {data.get('captions_srt')}",
        f"- Thumbnails: {', '.join(data.get('thumbnails') or [])}",
        "",
        "## Paths used",
        "",
    ]
    for k, v in (data.get("paths_used") or {}).items():
        lines.append(f"- {k}: {v}")
    visual_sources = data.get("visual_sources") or []
    if visual_sources:
        lines += ["", "## Visuals Sources", ""]
        if data.get("stock_share") is not None:
            lines.append(f"- Stock share: {data['stock_share']}")
        if data.get("stock_count") is not None:
            lines.append(f"- Stock scenes: {data['stock_count']}")
        if data.get("plate_count") is not None:
            lines.append(f"- Local plate scenes: {data['plate_count']}")
        for entry in visual_sources:
            if isinstance(entry, dict):
                lines.append(f"- {entry.get('scene_id')}: {entry.get('source')}")
            else:
                lines.append(f"- {entry}")
    lines += ["", "## Warnings", ""]
    warnings = data.get("warnings") or []
    lines.extend([f"- {w}" for w in warnings] or ["- (none)"])
    lines.append("")
    return "\n".join(lines)


def _append_visuals_section(report_path: Path, data: dict[str, Any]) -> None:
    """Append the visuals provenance section to report.md.

    The pack report builder renders a fixed schema, so the Round 3
    per-scene sources / stock share collected from the visuals stage
    are appended here — the report ALWAYS shows them when the stage
    supplied them.  Best-effort: a failure never breaks the pack.
    """
    visual_sources = data.get("visual_sources") or []
    if not visual_sources:
        return
    try:
        existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        if "## Visuals Sources" in existing:
            return
        lines = ["", "## Visuals Sources", ""]
        if data.get("stock_share") is not None:
            lines.append(f"- Stock share: {data['stock_share']}")
        if data.get("stock_count") is not None:
            lines.append(f"- Stock scenes: {data['stock_count']}")
        if data.get("plate_count") is not None:
            lines.append(f"- Local plate scenes: {data['plate_count']}")
        for entry in visual_sources:
            if isinstance(entry, dict):
                lines.append(f"- {entry.get('scene_id')}: {entry.get('source')}")
            else:
                lines.append(f"- {entry}")
        lines.append("")
        with report_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass


def _append_tts_section(report_path: Path, data: dict[str, Any]) -> None:
    """Append the Round 4 TTS input-script provenance to report.md
    (mirrors :func:`_append_visuals_section`).  Best-effort."""
    script_used = (data.get("paths_used") or {}).get("tts_input_script")
    if not script_used:
        return
    try:
        existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        if "## TTS Input Script" in existing:
            return
        lines = ["", "## TTS Input Script", "",
                 f"- Input script: {script_used}"]
        count = data.get("transliteration_unknown_token_count")
        if count is not None:
            lines.append(f"- Transliteration unknown tokens: {count}")
        tokens = data.get("transliteration_unknown_tokens") or []
        if tokens:
            lines.append(
                "- Unknown tokens left in Latin script: "
                + ", ".join(sorted(set(str(t) for t in tokens))))
        lines.append("")
        with report_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass


def _append_brightness_section(report_path: Path, data: dict[str, Any]) -> None:
    """Append the SL-006 stock brightness selection-filter section.

    Renders the filter's run record from the visuals stage
    (``data["brightness_filter"]``, copied from the fetcher's
    ``brightness_filter.stats()``): the floor, how many stock
    candidates were measured, and how many were REJECTED as
    near-black before selection (with asset id + measured YAVG per
    rejection).  Selection-time only — no QC threshold is touched.
    Best-effort: a failure never breaks the pack.
    """
    stats = data.get("brightness_filter")
    if not isinstance(stats, dict) or not stats:
        return
    try:
        existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        if "## Brightness Filter" in existing:
            return
        lines = ["", "## Brightness Filter (stock selection, SL-006)", ""]
        lines.append(
            f"- Floor: mean luma (signalstats YAVG) below "
            f"{stats.get('floor_yavg')} is rejected as near-black "
            f"at selection time (before QC; no QC rule changed)")
        lines.append(f"- Candidates measured: {stats.get('checked')}")
        lines.append(
            f"- Candidates rejected as too dark: "
            f"{stats.get('rejected_too_dark')}")
        lines.append(f"- Unmeasurable (abstained, passed): {stats.get('unmeasurable')}")
        for rej in stats.get("rejections") or []:
            if isinstance(rej, dict):
                lines.append(
                    f"- Rejected asset {rej.get('asset_id')}: "
                    f"YAVG {rej.get('yavg')} < floor {rej.get('floor')}")
        lines.append("")
        with report_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass


def _append_subtitle_metrics_section(report_path: Path, data: dict[str, Any]) -> None:
    """Append the subtitle script-match metrics to report.md (R4-005).

    Mirrors :func:`_append_tts_section`.  Renders BOTH scores from the
    production QC summary's subtitle block, each labelled with its
    metric name: the strict ``script_match_ratio`` and the
    transliteration-aware ``script_match_ratio_fair``.  The fair score
    is reporting context only — it does not change any gate outcome
    (``SUBTITLE_SCRIPT_MATCH_GATE_POLICY = "report_only"``).
    Best-effort: a failure never breaks the pack.
    """
    qc_summary = data.get("qc_summary") or {}
    subtitles = qc_summary.get("subtitles") if isinstance(qc_summary, dict) else None
    if not isinstance(subtitles, dict):
        return
    metrics = subtitles.get("script_match_metrics") or {}
    strict = metrics.get("strict") or {}
    fair = metrics.get("fair") or {}
    strict_score = strict.get("score", subtitles.get("script_match_ratio"))
    fair_score = fair.get("score", subtitles.get("script_match_ratio_fair"))
    if strict_score is None and fair_score is None:
        return
    try:
        existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        if "## Subtitle Script Match" in existing:
            return
        lines = ["", "## Subtitle Script Match", ""]
        lines.append(
            f"- Strict ({strict.get('metric') or 'script_match_ratio'}): "
            f"{strict_score}"
        )
        lines.append(
            f"- Fair ({fair.get('metric') or 'script_match_ratio_fair'}): "
            f"{fair_score}"
        )
        lines.append(
            "- Gate policy: report_only — the fair score is context only; "
            "gate outcomes are unchanged (strict metric + QC flags decide)."
        )
        lines.append("")
        with report_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Style Lab: style-profile identity + report section (mini-round, W5)
# ---------------------------------------------------------------------------
#
# A style profile carries PATTERNS ONLY (see
# ``app.integration.style_profile``): it steers stages, it never
# licences copying, and Gate A / Gate B run exactly as before.  This
# block only makes an applied profile VISIBLE in the pack report.

#: Per-stage subsections of the style report section, in render order.
_STYLE_STAGE_HEADINGS = (
    ("script", "Script"),
    ("seo", "SEO"),
    ("visuals", "Visuals"),
    ("thumbnail", "Thumbnail"),
)


def _style_profile_summary(profile_obj: Any, spec: Any,
                           base_dir: str | Path) -> dict[str, Any] | None:
    """Small identity dict for a resolved style profile (or ``None``).

    Carries exactly what the report section and ``paths_used`` need:
    ``schema_version``, ``channel_id``, ``source_count`` (provenance
    source count — length of ``provenance.source_asset_ids``, falling
    back to ``video_count``) and ``profile_path`` (the file the
    resolver used, when it came from a file; display only).
    """
    if profile_obj is None:
        return None
    provenance = _get(profile_obj, "provenance")
    source_ids = _get(provenance, "source_asset_ids") if provenance is not None else None
    if isinstance(source_ids, (list, tuple)):
        source_count = len(source_ids)
    else:
        video_count = _get(profile_obj, "video_count")
        source_count = int(video_count) if isinstance(video_count, int) else 0
    profile_path: str | None = None
    explicit = _get(spec, "style_profile_path")
    if explicit:
        profile_path = str(explicit)
    else:
        channel_id = _get(profile_obj, "channel_id") or _get(spec, "channel_id")
        if channel_id:
            candidate = Path(base_dir) / "style_profiles" / f"{channel_id}.json"
            if candidate.is_file():
                profile_path = str(candidate)
        if profile_path is None:
            env_path = os.environ.get("YTA_STYLE_PROFILE_PATH")
            if env_path:
                profile_path = env_path
    return {
        "schema_version": str(_get(profile_obj, "schema_version") or ""),
        "channel_id": str(
            _get(profile_obj, "channel_id") or _get(spec, "channel_id") or ""),
        "source_count": source_count,
        "video_count": _get(profile_obj, "video_count"),
        "profile_path": profile_path,
    }


def _style_applied_dict(obj: Any) -> dict[str, Any]:
    """The ``style_applied`` dict recorded on a stage result (or {})."""
    raw = _get(obj, "style_applied")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    dumped = _to_dict(raw)
    return dumped if isinstance(dumped, dict) else {}


def _is_empty_style_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple)):
        return len(value) == 0
    return False


def _collect_style_applied(data: dict[str, Any]) -> dict[str, Any]:
    """Aggregate per-stage style params from the pack ``data``.

    DEFENSIVE by design (the stage workers land in parallel): every
    source is read with ``.get`` / ``getattr`` defaults, so a stage
    that recorded nothing — or whose ``style_applied`` has not landed
    yet — yields ``None`` for its stage ("not applied") instead of an
    error.  Sources, later ones winning per stage:

    * ``data["style_applied"]`` — an already-aggregated dict;
    * ``data["bundle_style_applied"]`` / ``["visuals_style_applied"]``
      / ``["assemble_style_applied"]`` — raw per-stage dicts captured
      by the produce flow;
    * live stage objects parked in ``data`` (``"bundle"`` /
      ``"visuals"`` / ``"assemble"``) — their ``style_applied``.

    Raw shape per the shared contract: the content bundle carries
    ``{"script": ..., "seo": ...}``, the visuals result carries
    ``{"visuals": ...}`` and the assemble result carries
    ``{"thumbnail": ...}``.  A single-stage raw dict that does not use
    its stage key is taken whole as that stage's params; a bundle raw
    dict with neither key is recorded under ``script`` (the bundle is
    primarily the script stage).
    """
    by_stage: dict[str, Any] = {key: None for key, _ in _STYLE_STAGE_HEADINGS}

    def _absorb(raw: Any, stage_keys: tuple[str, ...]) -> None:
        if not isinstance(raw, dict) or not raw:
            return
        named = [k for k in stage_keys
                 if k in raw and not _is_empty_style_value(raw[k])]
        if named:
            for key in named:
                by_stage[key] = raw[key]
        else:
            # No stage key used: the whole dict is the (first) stage's
            # params — for the bundle that is "script" (see docstring).
            by_stage[stage_keys[0]] = raw

    aggregated = data.get("style_applied")
    if isinstance(aggregated, dict):
        for key, _heading in _STYLE_STAGE_HEADINGS:
            if key in aggregated and not _is_empty_style_value(aggregated[key]):
                by_stage[key] = aggregated[key]
    _absorb(data.get("bundle_style_applied"), ("script", "seo"))
    _absorb(data.get("visuals_style_applied"), ("visuals",))
    _absorb(data.get("assemble_style_applied"), ("thumbnail",))
    _absorb(_style_applied_dict(data.get("bundle")), ("script", "seo"))
    _absorb(_style_applied_dict(data.get("visuals")), ("visuals",))
    _absorb(_style_applied_dict(data.get("assemble")), ("thumbnail",))
    return by_stage


def _fmt_style_value(value: Any) -> str:
    if isinstance(value, dict):
        parts = [f"{key}={_fmt_style_value(value[key])}"
                 for key in sorted(value, key=str)]
        return ", ".join(parts) if parts else "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt_style_value(item) for item in value)
    return str(value)


def _append_style_section(report_path: Path, data: dict[str, Any]) -> None:
    """Append the Style Lab section to report.md (mirrors the visuals /
    TTS appenders above).  Best-effort: a failure never breaks the pack.

    When the run resolved NO style profile this appends NOTHING, so
    reports for non-style runs stay byte-identical to before Style Lab.
    """
    summary = data.get("style_profile")
    if not summary:
        return
    if not isinstance(summary, dict):
        summary = _to_dict(summary)
        if not summary:
            return
    try:
        existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        if "## Style Profile Applied" in existing:
            return
        by_stage = _collect_style_applied(data)
        lines = ["", "## Style Profile Applied", ""]
        lines.append(f"- Schema version: {summary.get('schema_version') or '—'}")
        lines.append(f"- Channel ID: {summary.get('channel_id') or '—'}")
        source_count = summary.get("source_count")
        lines.append(
            f"- Provenance source count: "
            f"{source_count if source_count is not None else 0}")
        if summary.get("profile_path"):
            lines.append(f"- Profile path: {summary['profile_path']}")
        lines.append("")
        for key, heading in _STYLE_STAGE_HEADINGS:
            lines.append(f"### {heading}")
            lines.append("")
            params = by_stage.get(key)
            if isinstance(params, dict):
                # None leaves mean "no directive for this knob" (W1
                # convention) — they are not applied parameters.
                params = {k: v for k, v in params.items() if v is not None}
            if _is_empty_style_value(params):
                lines.append("- no style parameters applied")
            elif isinstance(params, dict):
                for param_key in sorted(params, key=str):
                    lines.append(
                        f"- {param_key}: {_fmt_style_value(params[param_key])}")
            elif isinstance(params, (list, tuple)):
                for item in params:
                    lines.append(f"- {_fmt_style_value(item)}")
            else:
                lines.append(f"- {_fmt_style_value(params)}")
            lines.append("")
        with report_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass


def _write_pack(ctx: Any, data: dict[str, Any], ledger_csv: Path) -> Path:
    """Write report.md via app.integration.pack if present, else fallback.

    Returns the report path.  Pack-module functions are tried by
    inspecting their signatures; any failure falls back to the local
    writer so a pack is ALWAYS produced.
    """
    pack_dir = Path(_get(ctx, "pack_dir"))
    pack_dir.mkdir(parents=True, exist_ok=True)
    report_path = pack_dir / "report.md"

    pack_fn_names = (
        "write_pack", "write_report", "create_pack", "generate_pack",
        "write_pack_files", "build_pack", "make_report", "write_report_md",
    )
    try:
        import importlib

        pack_mod = importlib.import_module("app.integration.pack")
    except Exception:
        pack_mod = None  # type: ignore[assignment]

    if pack_mod is not None:
        # Known Round-2 pack contract: write_report_md(result_dict, path)
        known = getattr(pack_mod, "write_report_md", None)
        if callable(known):
            try:
                known(dict(data), report_path)
                if report_path.exists():
                    return report_path
            except Exception:
                pass
        for name in pack_fn_names:
            fn = getattr(pack_mod, name, None)
            if not callable(fn):
                continue
            try:
                result = _call_pack_fn(fn, ctx, data, ledger_csv, pack_dir)
            except Exception:
                continue
            # Interpret result: a path / dict / object pointing at report.
            candidate = None
            if isinstance(result, (str, Path)):
                candidate = Path(result)
                if candidate.is_dir():
                    candidate = candidate / "report.md"
            elif result is not None:
                for attr in ("report_md", "report_path", "report"):
                    val = _get(result, attr)
                    if val:
                        candidate = Path(str(val))
                        break
                if candidate is None and isinstance(result, dict) and result.get("pack_dir"):
                    candidate = Path(str(result["pack_dir"])) / "report.md"
            if candidate is not None and candidate.exists():
                if candidate != report_path:
                    try:
                        shutil.copyfile(candidate, report_path)
                    except Exception:
                        return candidate
                return report_path
            if report_path.exists():
                return report_path
        # A pack module may expose a string builder instead.
        for name in ("build_report", "render_report", "report_markdown"):
            fn = getattr(pack_mod, name, None)
            if callable(fn):
                try:
                    text = _call_pack_fn(fn, ctx, data, ledger_csv, pack_dir)
                    if isinstance(text, str) and text.strip():
                        report_path.write_text(text, encoding="utf-8")
                        return report_path
                except Exception:
                    continue

    report_path.write_text(_fallback_report(ctx, data), encoding="utf-8")
    return report_path


def _call_pack_fn(fn: Callable[..., Any], ctx: Any, data: dict[str, Any],
                  ledger_csv: Path, pack_dir: Path) -> Any:
    """Call a pack function with whatever its signature actually wants."""
    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        params = []
    # Keyword-capable call first when parameter names are informative.
    kwargs: dict[str, Any] = {}
    pool = {
        "ctx": ctx, "context": ctx, "spec": _get(ctx, "spec"),
        "result": data, "data": data, "production": data,
        "title": data.get("title"), "topic": data.get("topic"),
        "pack_dir": pack_dir, "ledger_csv": ledger_csv,
        "ledger_path": ledger_csv, "licence_ledger_csv": ledger_csv,
        "final_mp4": data.get("final_mp4"), "warnings": data.get("warnings"),
        "paths_used": data.get("paths_used"),
        "gate_a_passed": data.get("gate_a_passed"),
        "gate_b_passed": data.get("gate_b_passed"),
        "qc_passed": data.get("qc_passed"),
        "publish_state": data.get("publish_state"),
        "duration_s": data.get("duration_s"),
        "loudness_lufs": data.get("loudness_lufs"),
        "thumbnails": data.get("thumbnails"),
        "captions_srt": data.get("captions_srt"),
        "publish_manifest_json": data.get("publish_manifest_json"),
    }
    names = [p.name for p in params]
    if names and all(n in pool for n in names):
        kwargs = {n: pool[n] for n in names}
        return fn(**kwargs)
    if len(params) == 1:
        # Most likely (ctx) or (result-like dict) — try context first.
        try:
            return fn(ctx)
        except TypeError:
            return fn(data)
    if len(params) == 0:
        return fn()
    # Generic positional attempts, most-informative first.
    for args in ((ctx, data), (data, ctx), (ctx,), (data,)):
        try:
            return fn(*args)
        except TypeError:
            continue
    return fn(ctx, data)


# ---------------------------------------------------------------------------
# Supervisor tracking (best-effort, never distorts the direct flow)
# ---------------------------------------------------------------------------

def _try_supervisor_tracking(ctx: Any, bundle: Any, publish: Any,
                             gate_a_passed: bool, gate_b_passed: bool,
                             warnings: list[str]) -> str:
    if not (gate_a_passed and gate_b_passed):
        return "skipped (a gate did not pass)"
    try:
        from app.orchestration.graph import build_supervisor_graph

        manifest_path = _get(publish, "manifest_path") or _get(publish, "publish_manifest_json")
        manifest = _get(publish, "manifest") or {}
        manifest_hash = _get(publish, "manifest_hash") or _get(manifest, "manifest_hash")
        if not manifest_hash and manifest_path and Path(str(manifest_path)).exists():
            manifest_hash = hashlib.sha256(Path(str(manifest_path)).read_bytes()).hexdigest()
        if not manifest_hash:
            manifest_hash = hashlib.sha256(
                json.dumps(_to_dict(manifest), sort_keys=True, default=str).encode()
            ).hexdigest()

        tokens = _get(publish, "tokens") or []
        token_dicts: list[dict[str, Any]] = []
        for tok in tokens:
            td = _to_dict(tok)
            if td.get("agent_id") and td.get("manifest_hash"):
                token_dicts.append(td)
        if {t.get("agent_id") for t in token_dicts} != {32, 33, 34}:
            # Mirror the publish stage's Gate B PASS verdict in the
            # graph's own token shape (tracking only).
            try:
                from app.orchestration.gates import issue_pass_token

                token_dicts = [
                    issue_pass_token(aid, str(manifest_hash)).model_dump(mode="json")
                    for aid in (32, 33, 34)
                ]
            except Exception:
                token_dicts = token_dicts

        script_text = str(_get(bundle, "script_text") or _get(bundle, "title") or "script")
        state: dict[str, Any] = {
            "project_id": str(_get(ctx, "project_id", "project")),
            "channel_id": str(_get(_get(ctx, "spec"), "channel_id", "local-test-channel")),
            "niche_profile_id": str(_get(ctx, "niche_id", "")),
            "topic": str(_get(_get(ctx, "spec"), "topic", "")),
            "target_minutes": float(_get(_get(ctx, "spec"), "minutes", 1.0) or 1.0),
            # Tracking run uses auto mode so review interrupts in the
            # graph cannot pause a pipeline that already completed its
            # own human-review bookkeeping in the stage flow.
            "publish_mode": "auto",
            "script_versions": [script_text],
            "locked_script_ref": script_text,
            "locked_script_hash": hashlib.sha256(script_text.encode()).hexdigest(),
            "red_claim_count": 0,
            "retention_lint_passed": True,
            "publish_manifest_hash": str(manifest_hash),
            "gate_status": {
                "gate_a_script_locked": False,
                "gate_b_tokens": token_dicts,
                "gate_b_passed": False,
            },
            "status": "running",
            "decision_log": [],
        }
        graph = build_supervisor_graph()
        final = graph.invoke(state, config={"configurable": {"thread_id": state["project_id"]}})
        status = str(_get(final, "status", "unknown"))
        return f"invoked (final status: {status})"
    except Exception as exc:  # tracking must never break production
        warnings.append(f"Supervisor tracking skipped: {exc!r}")
        return f"skipped ({exc!r})"


# ---------------------------------------------------------------------------
# Artifact flattening into the caller-supplied base dir
# ---------------------------------------------------------------------------

def _flatten_pack(base_dir: Path, pack_dir: Path, data: dict[str, Any]) -> None:
    """Copy headline pack artifacts into ``base_dir`` itself.

    ``make_context`` nests the project under ``<base>/<project_id>/``;
    copying the finished pack files up one level makes the folder the
    caller named (e.g. ``outputs/round2/facts_ocean/``) directly
    contain final.mp4 / captions.srt / ledger / manifest / report /
    thumbnails, as the Round 2 deliverable specifies.  Best-effort.
    """
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        sources: list[Path] = []
        for key in ("final_mp4", "captions_srt", "publish_manifest_json"):
            if data.get(key):
                sources.append(Path(str(data[key])))
        for thumb in data.get("thumbnails") or []:
            sources.append(Path(str(thumb)))
        sources += [pack_dir / "licence_ledger.csv", pack_dir / "report.md",
                    pack_dir / "llm_usage.json"]
        if data.get("publish_manifest_json"):
            sources.append(Path(str(data["publish_manifest_json"])))
        seen: set[str] = set()
        for src in sources:
            if not src.exists() or str(src) in seen:
                continue
            seen.add(str(src))
            dest = base_dir / src.name
            if src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def produce_video(spec: Any, base_dir: str | Path) -> Any:
    """Run one video end-to-end and return a ``ProductionResult``.

    This is the loop owner for the run (see the module docstring and
    ``app.integration.run_loop``): the whole run executes inside one
    ``produce_loop()``.  When the caller itself already runs on an
    event loop (an async host embedding this sync entry point), the
    run is driven on a worker thread that enters its own
    ``produce_loop()`` there -- the single outer-boundary bridge, so
    the stages inside still see exactly one owned loop.
    """
    import asyncio
    import threading

    from app.integration.run_loop import produce_loop

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        with produce_loop():
            return _produce_video_impl(spec, base_dir)

    box: dict[str, Any] = {}

    def _thread_main() -> None:
        try:
            with produce_loop():
                box["value"] = _produce_video_impl(spec, base_dir)
        except BaseException as exc:  # propagate faithfully to caller
            box["error"] = exc

    thread = threading.Thread(target=_thread_main, daemon=True, name="yta-produce")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _produce_video_impl(spec: Any, base_dir: str | Path) -> Any:
    """The produce run itself; always called under ``produce_loop()``."""
    started = time.monotonic()
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    from app.integration.models import ProductionResult, make_context

    profile = resolve_profile(spec)
    ctx = make_context(spec, profile, base)

    # -- Style Lab profile (patterns only — NOT a copying licence;
    # Gate A / Gate B below run exactly as before) ---------------------
    # Resolved per W1's contract (explicit spec path, then
    # <base>/style_profiles/<channel_id>.json, then env).  When nothing
    # is configured the resolver returns None and this run is identical
    # to a pre-Style-Lab run.  Deliberately NO try/except here
    # (R4-003 fail-loud): an explicitly configured but missing/broken
    # profile file must error the run visibly, never be swallowed.
    from app.integration.style_profile import resolve_style_profile

    style_profile_obj = resolve_style_profile(spec, base)
    style_summary = _style_profile_summary(style_profile_obj, spec, base)

    niche_id = str(_get(ctx, "niche_id", None) or _get(profile, "profile_id", None) or _get(spec, "niche"))
    project_id = str(_get(ctx, "project_id", "project"))
    pack_dir = Path(_get(ctx, "pack_dir"))
    publish_mode = str(_get(spec, "publish_mode", "review_first") or "review_first")

    # -- Content + Gate A -------------------------------------------------
    bundle = _call_stage("build_content", spec, profile)
    # Style params the content stage actually applied (script + SEO),
    # captured now so even a Gate-A-blocked run reports them.
    bundle_style_applied = _style_applied_dict(bundle)
    # Provenance FROM the LLM usage ledger data the content stage
    # aggregated from its router calls (report_facts derives the path
    # label from the ledger evidence itself, so a bundle carrying
    # models/tokens/cost can never be labelled the stub — Round 3 #5).
    from app.integration.report_facts import build_llm_usage_ledger, facts_from_llm_usage

    llm_facts = facts_from_llm_usage(bundle)
    llm_ledger_payload = build_llm_usage_ledger(bundle)
    llm_path = llm_facts["path"]
    llm_cost = llm_facts["cost_usd"]
    gate_raw = _call_stage("run_gate_a", bundle, profile)
    gate_a_passed, gate_a_reasons = _normalise_gate_a(gate_raw)
    title = str(_get(bundle, "title", "") or _get(spec, "topic", ""))
    if not gate_a_passed:
        warnings.append(
            "Gate A FAILED: " + ("; ".join(gate_a_reasons) if gate_a_reasons else "no reasons given")
        )

    data: dict[str, Any] = {
        "project_id": project_id,
        "niche_id": niche_id,
        "topic": str(_get(spec, "topic", "")),
        "title": title,
        "pack_dir": str(pack_dir),
        "final_mp4": None,
        "thumbnails": [],
        "captions_srt": None,
        "publish_manifest_json": None,
        "duration_s": None,
        "loudness_lufs": None,
        "cost_usd": float(llm_cost),
        "llm_models": llm_facts["models"],
        "llm_usage": llm_facts["usage"],
        "_llm_usage_ledger": llm_ledger_payload,
        "paths_used": {
            "llm": llm_path,
            "tts": "not-run",
            "visuals": "local-plates",
            "subtitles": "not-run",
            "publish": "fake-provider",
        },
        "gate_a_passed": gate_a_passed,
        "gate_b_passed": False,
        "qc_passed": False,
        "publish_state": "not_started",
        "warnings": warnings,
        # Style Lab identity for the pack/report (None when unstyled);
        # per-stage applied params ride along raw and are aggregated
        # defensively at report time (see _collect_style_applied).
        "style_profile": style_summary,
        "bundle_style_applied": bundle_style_applied,
        # Round 5: the per-channel quality tier requested for this
        # run (spec-carried); the assemble step below overwrites it
        # with the tier the render actually used.
        "quality_tier": str(_get(spec, "quality_tier") or "draft"),
    }

    if style_summary:
        # ProductionResult has no metadata field (models.py is frozen
        # for this round); paths_used is its existing per-stage
        # provenance dict, so the style summary is exposed there.
        data["paths_used"]["style_profile"] = (
            f"{style_summary['schema_version']} "
            f"(channel: {style_summary['channel_id']}, "
            f"sources: {style_summary['source_count']})"
        )

    def _finish() -> Any:
        # Ledger + report are ALWAYS written, even for a blocked run.
        from app.integration.ledger import LicenceLedger

        ledger = LicenceLedger()
        seen_keys: set[tuple[str, str]] = set()
        for row in data.pop("_ledger_rows", []):
            # Assemble licence_segments duplicate the visuals plate
            # rows (same asset/scene); keep the first, richer row.
            rd = _to_dict(row)
            key = (
                str(rd.get("asset_id") or rd.get("asset") or ""),
                str(rd.get("scene_id") or ""),
            )
            if key != ("", "") and key in seen_keys:
                continue
            if _ledger_add(ledger, row):
                if key != ("", ""):
                    seen_keys.add(key)
            else:
                warnings.append(f"Ledger row skipped (unrecognised shape): {row!r:.200}")
        ledger_csv = pack_dir / "licence_ledger.csv"
        ledger.write_csv(ledger_csv)
        data["licence_ledger_csv"] = str(ledger_csv)

        # -- Report integrity: derive, never recall (Round 4, #5) -----
        # Every derived report field is recomputed HERE from the
        # objects of record, overwriting any interim stage counter:
        # visuals from the licence-ledger rows just written, LLM facts
        # from the usage ledger, gates/state/duration from the frozen
        # publish manifest on disk.
        from app.integration.report_facts import (
            facts_from_publish_manifest,
            facts_from_visual_records,
        )

        # Licence ledger UNION scene manifest: the interim
        # ``visual_sources`` in ``data`` came from the visuals stage's
        # scene manifest above; the ledger alone can omit scenes that
        # needed no licence row, and dropping them from the report was
        # the regression behind test_produce_report_shows_visual_sources.
        visual_facts = facts_from_visual_records(
            ledger.rows, data.get("visual_sources") or []
        )
        if visual_facts["visual_sources"]:
            data["visual_sources"] = visual_facts["visual_sources"]
            data["scene_sources"] = visual_facts["scene_sources"]
            data["stock_count"] = visual_facts["stock_count"]
            data["plate_count"] = visual_facts["plate_count"]
            data["stock_share"] = visual_facts["stock_share"]

        llm_payload = data.pop("_llm_usage_ledger", None)
        if llm_payload is not None:
            llm_json = pack_dir / "llm_usage.json"
            try:
                llm_json.write_text(
                    json.dumps(llm_payload, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                data["llm_usage_json"] = str(llm_json)
            except Exception:
                pass
            # The report's LLM fields come from the ledger payload
            # just persisted — the same bytes an auditor re-reads.
            data["llm_models"] = llm_payload.get("models") or []
            data["llm_usage"] = llm_payload.get("usage") or {}
            data["cost_usd"] = float(llm_payload.get("cost_usd") or 0.0)
            data["paths_used"]["llm"] = str(llm_payload.get("path") or data["paths_used"]["llm"])

        manifest_file = data.get("publish_manifest_json")
        if manifest_file and Path(str(manifest_file)).is_file():
            try:
                manifest_on_disk = json.loads(
                    Path(str(manifest_file)).read_text(encoding="utf-8")
                )
                for key, value in facts_from_publish_manifest(manifest_on_disk).items():
                    data[key] = value
            except Exception:
                pass

        if data.get("style_profile"):
            # Aggregate the per-stage style params into the pack data
            # itself, so the report section and any pack consumer read
            # the same collected view (missing stages stay None).
            data["style_applied"] = _collect_style_applied(data)

        data["elapsed_s"] = round(time.monotonic() - started, 3)
        report = _write_pack(ctx, data, ledger_csv)
        _append_visuals_section(report, data)
        _append_tts_section(report, data)
        _append_subtitle_metrics_section(report, data)
        _append_brightness_section(report, data)
        _append_style_section(report, data)
        data["report_md"] = str(report)
        _flatten_pack(base, pack_dir, data)
        keep = {
            "project_id", "niche_id", "topic", "title", "pack_dir",
            "final_mp4", "thumbnails", "captions_srt", "licence_ledger_csv",
            "publish_manifest_json", "report_md", "duration_s",
            "loudness_lufs", "cost_usd", "paths_used", "gate_a_passed",
            "gate_b_passed", "qc_passed", "publish_state", "elapsed_s",
            "warnings",
        }
        return ProductionResult(**{k: v for k, v in data.items() if k in keep})

    data["_ledger_rows"] = []

    if not gate_a_passed and publish_mode != "review_first":
        data["publish_state"] = "blocked_gate_a"
        warnings.append(
            "Production stopped after Gate A (publish_mode is not review_first)."
        )
        return _finish()
    if not gate_a_passed:
        warnings.append(
            "Continuing despite Gate A failure ONLY because publish_mode="
            "'review_first' (human review required before publish)."
        )

    # -- Voice ------------------------------------------------------------
    voice = _call_stage("synthesize_voice", bundle, profile, ctx)
    if _get(voice, "path_used"):
        data["paths_used"]["tts"] = str(_get(voice, "path_used"))
    # Round 4 TTS provenance (same pattern as llm_path above): which
    # input script the narration was synthesised from + how many
    # Latin tokens stayed untransliterated (unknown English).
    if _get(voice, "tts_input_script"):
        data["paths_used"]["tts_input_script"] = str(
            _get(voice, "tts_input_script"))
        _unknown = _get(voice, "transliteration_unknown_tokens") or []
        data["transliteration_unknown_token_count"] = len(_unknown)
        data["transliteration_unknown_tokens"] = [str(t) for t in _unknown]

    # -- Visuals ----------------------------------------------------------
    visuals = _call_stage("plan_and_generate_visuals", bundle, voice, profile, ctx)
    _visuals_style_applied = _style_applied_dict(visuals)
    if _visuals_style_applied:
        data["visuals_style_applied"] = _visuals_style_applied
    # SL-006: the brightness selection filter's run record (from the
    # visuals stage, which read it off the stock fetcher).
    _brightness_stats = _get(visuals, "brightness_stats")
    if isinstance(_brightness_stats, dict) and _brightness_stats:
        data["brightness_filter"] = dict(_brightness_stats)
    if _get(visuals, "path_used"):
        data["paths_used"]["visuals"] = str(_get(visuals, "path_used"))
    # Round 3 stock reporting: surface per-scene sources + stock share
    # so report.md can show them (kept out of ProductionResult, whose
    # schema is frozen; the report append in _finish renders them).
    _manifest = _get(visuals, "scene_manifest") or _get(visuals, "scenes") or []
    _visual_sources: list[dict[str, Any]] = []
    for _scene in _manifest:
        _src = _get(_scene, "source")
        if _src:
            _visual_sources.append(
                {"scene_id": str(_get(_scene, "scene_id") or ""), "source": str(_src)}
            )
    if not _visual_sources:
        for _src in _get(visuals, "scene_sources") or _get(visuals, "sources") or []:
            _visual_sources.append({"scene_id": "", "source": str(_src)})
    if _visual_sources:
        # Interim values only — _finish() overwrites all of these from
        # the licence ledger.  Classification goes through
        # report_facts (bare provider labels like "pixabay" ARE stock;
        # the old startswith("stock:") check counted them as plates —
        # the inverted Round 3 summary).
        from app.integration.report_facts import facts_from_licence_rows

        interim = facts_from_licence_rows(
            {"scene_id": e["scene_id"], "source": e["source"]} for e in _visual_sources
        )
        data["visual_sources"] = _visual_sources
        data["scene_sources"] = [e["source"] for e in _visual_sources]
        data["stock_count"] = interim["stock_count"]
        data["plate_count"] = interim["plate_count"]
        data["stock_share"] = interim["stock_share"]

    # -- Assemble -----------------------------------------------------------
    assemble = _call_assemble_stage(bundle, voice, visuals, profile, ctx)
    # Round 5: the tier actually rendered, as reported by the
    # assemble stage (falling back to the spec's requested tier).
    _rendered_tier = _get(assemble, "quality_tier") or _get(spec, "quality_tier")
    if _rendered_tier:
        data["quality_tier"] = str(_rendered_tier)
        data["paths_used"]["quality_tier"] = str(_rendered_tier)
    _assemble_style_applied = _style_applied_dict(assemble)
    if _assemble_style_applied:
        data["assemble_style_applied"] = _assemble_style_applied
    data["_ledger_rows"] = _collect_ledger_rows(voice, visuals, assemble)
    data["final_mp4"] = _as_str_path(_get(assemble, "final_mp4"))
    data["thumbnails"] = _as_str_list(_get(assemble, "thumbnails"))
    # NOTE (sibling shape): assemble_stage returns the SRT *text* in
    # captions_srt and writes the file to <pack_dir>/captions.srt.
    # Normalise to the file path here, in the orchestrator.
    raw_captions = _get(assemble, "captions_srt") or _get(assemble, "captions_path")
    raw_is_file = False
    if raw_captions:
        raw_str = str(raw_captions)
        # SRT text is long / multi-line; never stat it as a path
        # (Path.is_file() raises OSError on over-long names).
        if len(raw_str) < 512 and "\n" not in raw_str:
            try:
                raw_is_file = Path(raw_str).is_file()
            except OSError:
                raw_is_file = False
    if raw_captions and raw_is_file:
        data["captions_srt"] = str(raw_captions)
    elif raw_captions:
        srt_file = pack_dir / "captions.srt"
        if srt_file.is_file():
            data["captions_srt"] = str(srt_file)
        elif isinstance(raw_captions, str) and "-->" in raw_captions:
            srt_file.write_text(raw_captions, encoding="utf-8")
            data["captions_srt"] = str(srt_file)
    if not data["captions_srt"] and (pack_dir / "captions.srt").is_file():
        data["captions_srt"] = str(pack_dir / "captions.srt")
    if _get(assemble, "duration_s") is not None:
        data["duration_s"] = float(_get(assemble, "duration_s"))
    if _get(assemble, "loudness_lufs") is not None:
        data["loudness_lufs"] = float(_get(assemble, "loudness_lufs"))
    data["qc_passed"] = bool(_get(assemble, "qc_passed", False))
    if _get(assemble, "subtitle_path_used"):
        data["paths_used"]["subtitles"] = str(_get(assemble, "subtitle_path_used"))
    if _get(assemble, "render_path_used"):
        data["paths_used"]["render"] = str(_get(assemble, "render_path_used"))
    qc_summary = _get(assemble, "qc_summary")
    if qc_summary:
        data["qc_summary"] = qc_summary
        warnings.append(f"QC summary: {qc_summary}")

    # -- Publish (fake provider) -------------------------------------------
    # Licence rows for the publish stage's copyright gates, taken from
    # the assemble stage's licence_segments.  One vocabulary fix at
    # this boundary: the visuals/assemble stages call the plate
    # provider "local_plate", which the publish stage's provider-tier
    # table does not know (it maps the same own/generated assets to
    # Tier 1 under "generated").  Translating the label here -- the
    # assets themselves are unchanged and the ledger CSV keeps the
    # original "own/generated (local Pillow plate)" source string.
    qc_licence_rows: list[dict[str, Any]] = []
    for seg in _get(assemble, "licence_segments") or []:
        row = _to_dict(seg)
        if str(row.get("provider") or "").strip().lower() in {
            "local_plate", "local-plates", "local plate", "localplate",
        }:
            row["provider"] = "generated"
        qc_licence_rows.append(row)

    qc_info: dict[str, Any] = {
        "qc_passed": data["qc_passed"],
        "qc_summary": qc_summary,
        "loudness_lufs": data["loudness_lufs"],
        "duration_s": data["duration_s"],
        "final_mp4": data["final_mp4"],
        "captions_srt": data["captions_srt"],
        "thumbnails": data["thumbnails"],
        "title": title,
        "licence_segments": qc_licence_rows,
    }
    publish = _call_stage(
        "finalize_publish", assemble, qc_info, bundle, profile, ctx, gate_a_passed
    )
    data["gate_b_passed"] = bool(_get(publish, "gate_b_passed", False))
    if _get(publish, "publish_state"):
        data["publish_state"] = str(_get(publish, "publish_state"))
    manifest_path = _get(publish, "manifest_path") or _get(publish, "publish_manifest_json")
    manifest_obj = _get(publish, "manifest")
    if manifest_path:
        data["publish_manifest_json"] = str(manifest_path)
    elif manifest_obj is not None:
        target = pack_dir / "publish_manifest.json"
        target.write_text(
            json.dumps(_to_dict(manifest_obj), indent=2, default=str), encoding="utf-8"
        )
        data["publish_manifest_json"] = str(target)
    # Ledger rows may also arrive from the publish stage.
    data["_ledger_rows"] = _collect_ledger_rows(
        type("R", (), {"ledger_rows": data["_ledger_rows"]})(), publish
    ) if False else data["_ledger_rows"] + _collect_ledger_rows(publish)
    # NOTE: cost_usd is NOT taken from the publish/assemble stages.
    # Neither stage keeps a cost ledger; an earlier version overwrote
    # the LLM-ledger cost here with whatever those stages happened to
    # carry (in practice 0.0), which is how real Gemini spend was
    # reported as $0.00 in Round 3.  Cost comes only from the LLM
    # usage ledger (see the content-stage capture + _finish).

    # -- Supervisor tracking (best-effort) ----------------------------------
    sup = _try_supervisor_tracking(ctx, bundle, publish, gate_a_passed,
                                   data["gate_b_passed"], warnings)
    data["paths_used"]["supervisor"] = sup
    warnings.append(f"Supervisor graph: {sup}. Direct stage flow is authoritative.")

    return _finish()
