"""Report facts derived ONLY from ledger / manifest objects.

Round 3 fragility #5 (see ``outputs/round3/phase0/PHASE0_REPORT.md``
§5): the produce layer's auto-generated ``report.md`` fields
contradicted the packs' own ledgers — it claimed
``llm=deterministic-stub`` / ``$0.00`` for scripts that were really
Gemini (ledger cost $0.059924 / $0.006021) and printed an inverted
stock/plate summary (0 stock / all plates) against licence ledgers
that said the opposite.

Structural fix: every *derived* field in the report is computed here,
at report time, from the objects of record — never from parallel
in-memory counters carried alongside them:

* visuals (per-scene sources, stock/plate counts, stock share)
  ← the :class:`~app.integration.ledger.LicenceLedger` rows that are
  about to be written to ``licence_ledger.csv``, unioned with the
  visuals stage's scene manifest (a licence ledger need not carry a
  row for scenes that required no licence — see
  :func:`facts_from_visual_records`);
* LLM provenance (path label, models, token counts, cost)
  ← the LLM usage ledger: per-call entries when the bundle carries
  them (``llm_ledger_entries`` — the router's ``LLMCallEntry`` rows),
  otherwise the bundle's ledger aggregates (``llm_models`` /
  ``llm_usage`` / ``llm_cost_usd``, which
  :mod:`app.integration.content` itself sums from those entries);
* gates / publish state / duration
  ← the frozen ``publish_manifest.json`` dict when one exists.

The functions here are pure: same ledger in, same facts out, no stage
state consulted.  A report built from these facts cannot diverge from
the CSV/JSON files sitting next to it in the pack — and the auditor
test (``tests/integration/test_report_integrity.py``) recomputes the
same figures from the on-disk files with independent code and asserts
equality.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "LLM_PATH_GEMINI_LIVE",
    "LLM_PATH_STUB",
    "build_llm_usage_ledger",
    "classify_scene_source",
    "facts_from_licence_rows",
    "facts_from_llm_usage",
    "facts_from_publish_manifest",
    "facts_from_visual_records",
]

LLM_PATH_STUB = "deterministic-stub"
LLM_PATH_GEMINI_LIVE = "gemini-live"

#: Provider tokens that mark a scene row as stock-sourced.  Mirrors
#: the vocabulary the stages actually write into ledger ``source``
#: fields ("Pixabay", "Pixabay stock video", "pixabay",
#: "stock:pixabay:123", "pexels", ...) — see
#: ``stock_ledger._STOCK_PROVIDER_NAMES``.
_STOCK_TOKENS = ("pixabay", "pexels", "shutterstock", "getty", "stock")
#: Markers for locally generated plate scenes.
_PLATE_TOKENS = ("plate", "pillow", "local", "own/generated", "own work", "generated")


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if obj is None:
            break
        value = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
        if value is not None and value != "":
            return value
    return default


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    dump = getattr(row, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except Exception:
            pass
    if hasattr(row, "to_csv_dict"):
        try:
            return dict(row.to_csv_dict())
        except Exception:
            pass
    if hasattr(row, "__dict__"):
        return dict(vars(row))
    return {}


# ---------------------------------------------------------------------------
# Visuals facts ← licence ledger
# ---------------------------------------------------------------------------

def classify_scene_source(source: Any) -> str:
    """Classify one ledger ``source`` string: "stock" | "plate" | "other".

    Stock wins first: "Pixabay stock video" contains both vocabularies
    in spirit but IS stock.  Bare provider labels ("pixabay") are stock
    — the Round 3 bug was a classifier that only recognised the
    ``"stock:"`` prefix and so counted every bare-label stock scene as
    a local plate.
    """
    text = str(source or "").strip().lower()
    if not text:
        return "other"
    if text.startswith("stock:") or any(tok in text for tok in _STOCK_TOKENS):
        return "stock"
    if any(tok in text for tok in _PLATE_TOKENS):
        return "plate"
    return "other"


def facts_from_licence_rows(rows: Iterable[Any]) -> dict[str, Any]:
    """Visuals facts from licence-ledger rows (the CSV of record).

    Only scene rows count: whole-video rows (``scene_id`` "" / "all" —
    voice, music) are not scenes.  ``stock_share`` is
    ``stock / (stock + plate)`` — scenes classified "other" appear in
    ``visual_sources`` but in neither counter.
    """
    visual_sources: list[dict[str, str]] = []
    stock_count = 0
    plate_count = 0
    for raw in rows or []:
        row = _row_dict(raw)
        scene_id = str(row.get("scene_id") or "")
        if scene_id.strip().lower() in ("", "all"):
            continue
        source = str(row.get("source") or "")
        visual_sources.append({"scene_id": scene_id, "source": source})
        kind = classify_scene_source(source)
        if kind == "stock":
            stock_count += 1
        elif kind == "plate":
            plate_count += 1
    classified = stock_count + plate_count
    return {
        "visual_sources": visual_sources,
        "scene_sources": [e["source"] for e in visual_sources],
        "stock_count": stock_count,
        "plate_count": plate_count,
        "stock_share": round(stock_count / classified, 4) if classified else 0.0,
    }


def facts_from_visual_records(
    licence_rows: Iterable[Any],
    scene_records: Iterable[Any] = (),
) -> dict[str, Any]:
    """Visuals facts from the licence ledger UNION the scene manifest.

    The licence ledger is the record of record for licensed assets,
    but it does not necessarily carry a row for every scene: a scene
    rendered as a local plate needs no licence, and stage contracts
    have historically emitted ledger rows only for externally sourced
    assets (the sibling stock test's fake does exactly this — one
    stock ledger row for a two-scene manifest).  Deriving the report's
    per-scene sources from the ledger alone therefore DROPS such
    scenes from the report entirely (that regression failed
    ``test_produce_report_shows_visual_sources``).

    The scene manifest (``scene_manifest.json``, written by the
    visuals stage) is the other manifest object of record for a run.
    This function unions the two, keyed by ``scene_id``:

    * a scene present in the licence ledger keeps the LEDGER's source
      string (the legal record wins on disagreement);
    * a scene present only in the scene manifest is appended with the
      manifest's source string;
    * unnamed manifest entries (``scene_id`` "") are keyed by source
      string so the ``scene_sources`` fallback shape still unions.

    Counts and stock share are recomputed from the merged scene list —
    never taken from either stage's aggregate counters.
    """
    merged: list[dict[str, str]] = []
    seen: set[str] = set()

    def _key(scene_id: str, source: str) -> str:
        return scene_id if scene_id else f"\x00{source}"

    for entry in facts_from_licence_rows(licence_rows)["visual_sources"]:
        merged.append(entry)
        seen.add(_key(entry["scene_id"], entry["source"]))
    for raw in scene_records or []:
        record = _row_dict(raw)
        scene_id = str(record.get("scene_id") or "")
        if scene_id.strip().lower() == "all":
            continue
        source = str(record.get("source") or "")
        if not source:
            continue
        key = _key(scene_id, source)
        if key in seen:
            continue
        seen.add(key)
        merged.append({"scene_id": scene_id, "source": source})

    stock_count = sum(
        1 for e in merged if classify_scene_source(e["source"]) == "stock")
    plate_count = sum(
        1 for e in merged if classify_scene_source(e["source"]) == "plate")
    classified = stock_count + plate_count
    return {
        "visual_sources": merged,
        "scene_sources": [e["source"] for e in merged],
        "stock_count": stock_count,
        "plate_count": plate_count,
        "stock_share": round(stock_count / classified, 4) if classified else 0.0,
    }


# ---------------------------------------------------------------------------
# LLM facts ← LLM usage ledger
# ---------------------------------------------------------------------------

def _normalise_usage(usage: Any) -> dict[str, int]:
    """Token totals from a bundle usage dict, either bucket vocabulary.

    Accepts the ContentBundle aggregate shape
    (``input_tokens`` / ``output_tokens`` / ``calls``) and the raw
    LLMCallEntry bucket shape (``input_uncached`` / ``input_cached`` /
    ``input_cache_write`` / ``output`` / ``reasoning``).
    """
    data = usage if isinstance(usage, Mapping) else {}
    if "input_tokens" in data or "output_tokens" in data:
        return {
            "input_tokens": int(data.get("input_tokens") or 0),
            "output_tokens": int(data.get("output_tokens") or 0),
            "calls": int(data.get("calls") or 0),
        }
    return {
        "input_tokens": int(
            (data.get("input_uncached") or 0)
            + (data.get("input_cached") or 0)
            + (data.get("input_cache_write") or 0)
        ),
        "output_tokens": int((data.get("output") or 0) + (data.get("reasoning") or 0)),
        "calls": int(data.get("calls") or 0),
    }


def _facts_from_entries(entries: list[Any]) -> dict[str, Any] | None:
    """Recompute LLM facts from per-call ledger entries, or None.

    Only ``status == "ok"`` rows produced script content (failed /
    refused attempts are spend-visibility rows with zero cost in the
    router ledger); they alone define models / tokens / cost here,
    exactly as :mod:`app.integration.content` aggregates its bundle
    fields from successful router results.
    """
    ok_rows = []
    for raw in entries:
        row = _row_dict(raw)
        if str(row.get("status") or "ok") == "ok":
            ok_rows.append(row)
    if not ok_rows:
        return None
    models: list[str] = []
    input_tokens = 0
    output_tokens = 0
    cost = 0.0
    for row in ok_rows:
        model = str(row.get("model") or "")
        if model and model not in models:
            models.append(model)
        input_tokens += int(
            (row.get("input_uncached") or 0)
            + (row.get("input_cached") or 0)
            + (row.get("input_cache_write") or 0)
        )
        output_tokens += int((row.get("output") or 0) + (row.get("reasoning") or 0))
        cost += float(row.get("cost_usd") or 0.0)
    return {
        "models": models,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "calls": len(ok_rows),
        },
        "cost_usd": round(cost, 6),
    }


def facts_from_llm_usage(bundle: Any) -> dict[str, Any]:
    """LLM provenance facts from the bundle's usage-ledger data.

    Returns ``{"path", "models", "usage", "cost_usd"}``.  The path
    label is DERIVED from the ledger evidence, not merely copied from
    the bundle's declared label: a bundle that carries models, token
    usage or cost cannot be reported as the deterministic stub (that
    exact contradiction was Round 3 fragility #5).  A declared live
    label (anything mentioning "live" / "real" / a provider name) is
    kept; otherwise live evidence maps to ``"gemini-live"``, the only
    live path this pipeline has.
    """
    entries = _get(bundle, "llm_ledger_entries", "ledger_entries")
    entry_facts = _facts_from_entries(list(entries)) if entries else None
    if entry_facts is not None:
        models = entry_facts["models"]
        usage = entry_facts["usage"]
        cost = entry_facts["cost_usd"]
    else:
        models = [str(m) for m in (_get(bundle, "llm_models", default=[]) or []) if m]
        usage = _normalise_usage(_get(bundle, "llm_usage", default={}))
        raw_cost = _get(bundle, "llm_cost_usd", "cost_usd")
        cost = round(float(raw_cost), 6) if isinstance(raw_cost, (int, float)) else 0.0
        if usage["calls"] == 0 and models:
            usage["calls"] = len(models)

    declared = str(_get(bundle, "llm_path", "path", default="") or "").strip()
    has_live_evidence = bool(models) or cost > 0 or usage["calls"] > 0 \
        or usage["input_tokens"] > 0 or usage["output_tokens"] > 0
    if has_live_evidence:
        low = declared.lower()
        path = declared if declared and low != LLM_PATH_STUB else LLM_PATH_GEMINI_LIVE
    else:
        path = declared or LLM_PATH_STUB
    return {"path": path, "models": models, "usage": usage, "cost_usd": cost}


def build_llm_usage_ledger(bundle: Any) -> dict[str, Any]:
    """The durable ``llm_usage.json`` pack ledger for one run.

    Written next to ``licence_ledger.csv`` so an auditor can recompute
    the report's LLM figures from a file, not from memory.  Per-call
    entries are transcribed when the bundle carries them.
    """
    facts = facts_from_llm_usage(bundle)
    ledger: dict[str, Any] = {
        "path": facts["path"],
        "models": facts["models"],
        "usage": facts["usage"],
        "cost_usd": facts["cost_usd"],
    }
    entries = _get(bundle, "llm_ledger_entries", "ledger_entries")
    if entries:
        ledger["entries"] = [_row_dict(e) for e in entries]
    return ledger


# ---------------------------------------------------------------------------
# Gate / publish facts ← publish manifest
# ---------------------------------------------------------------------------

def facts_from_publish_manifest(manifest: Any) -> dict[str, Any]:
    """Gate verdicts, publish state and duration from the manifest.

    The frozen manifest is the record the publish stage itself froze
    (its ``duration_seconds`` is ffprobe-measured there), so the report
    takes these fields from it whenever a manifest exists.  Missing
    keys are simply absent from the result (callers keep their stage
    values for those).
    """
    data = manifest if isinstance(manifest, Mapping) else _row_dict(manifest)
    facts: dict[str, Any] = {}
    if not data:
        return facts
    if data.get("gate_a_passed") is not None:
        facts["gate_a_passed"] = bool(data["gate_a_passed"])
    gate_b = data.get("gate_b")
    if isinstance(gate_b, Mapping) and gate_b.get("passed") is not None:
        facts["gate_b_passed"] = bool(gate_b["passed"])
    if isinstance(gate_b, Mapping):
        # Shared Gate B verdict labelling (Round 6 closure, W7): the
        # report takes the manifest's canonical verdict label +
        # typed reason codes verbatim, so a conditional release is
        # reported as ``conditional_pass`` — never as a bare pass the
        # publish finaliser would contradict.
        verdict = gate_b.get("verdict")
        if not verdict and gate_b.get("outcome"):
            outcome = str(gate_b["outcome"])
            if gate_b.get("passed") and outcome == "conditional":
                verdict = "conditional_pass"
            elif gate_b.get("passed"):
                verdict = "pass"
            elif outcome == "conditional":
                verdict = "conditional_blocked"
            else:
                verdict = "block"
        if verdict:
            facts["gate_b_verdict"] = str(verdict)
        if gate_b.get("outcome"):
            facts["gate_b_outcome"] = str(gate_b["outcome"])
        if gate_b.get("reason_codes"):
            facts["gate_b_reason_codes"] = [
                str(c) for c in gate_b["reason_codes"]]
    if data.get("publish_state"):
        facts["publish_state"] = str(data["publish_state"])
    duration = data.get("duration_seconds", data.get("duration_s"))
    if isinstance(duration, (int, float)) and duration > 0:
        facts["duration_s"] = float(duration)
    return facts
