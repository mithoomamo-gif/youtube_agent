"""Pack I/O: the deliverable files written into a project's ``pack/``.

Two helpers, deliberately boring and dependency-free:

* :func:`write_json` — atomic-ish JSON write (temp file in the same
  directory + :func:`os.replace`), UTF-8, ``ensure_ascii=False`` so Urdu
  titles stay human-readable in the file.
* :func:`write_report_md` — renders the human-readable production
  report (``report.md``) from a result dict (typically
  ``ProductionResult.model_dump()``): title, niche, topic, duration,
  loudness, cost, elapsed, the ``paths_used`` table, gate verdicts, a
  QC summary, the file list and any warnings.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

__all__ = [
    "write_json",
    "write_report_md",
]


# ---------------------------------------------------------------------------
# Generic writers
# ---------------------------------------------------------------------------

def _atomic_write_text(path: Path, text: str) -> Path:
    """Write ``text`` to ``path`` atomically-ish and return ``path``.

    Writes a temp file in the *same* directory and renames it over the
    target, so a crash mid-write never leaves a half-written pack file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def _jsonable(data: Any) -> Any:
    """Recursively convert models / dataclasses / Paths to JSON data."""
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    if is_dataclass(data) and not isinstance(data, type):
        return {key: _jsonable(value) for key, value in asdict(data).items()}
    if isinstance(data, dict):
        return {key: _jsonable(value) for key, value in data.items()}
    if isinstance(data, (list, tuple, set, frozenset)):
        return [_jsonable(value) for value in data]
    if isinstance(data, Path):
        return str(data)
    return data


def write_json(path: str | Path, data: Any) -> Path:
    """Write ``data`` as JSON to ``path`` and return the path written.

    UTF-8 with ``ensure_ascii=False`` (Urdu / non-ASCII text is kept
    as-is), pretty-printed with a 2-space indent. Pydantic models and
    dataclasses are converted first; everything else must already be
    JSON-serialisable.
    """
    target = Path(path)
    text = json.dumps(_jsonable(data), ensure_ascii=False, indent=2) + "\n"
    return _atomic_write_text(target, text)


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def _as_result_dict(result_data: Any) -> dict[str, Any]:
    if isinstance(result_data, dict):
        return dict(result_data)
    converted = _jsonable(result_data)
    if isinstance(converted, dict):
        return converted
    raise TypeError(
        "write_report_md expects a dict (or a model/dataclass that "
        f"converts to one), got {type(result_data).__name__}"
    )


def _md(value: Any) -> str:
    """Escape a value for a Markdown table cell / inline use."""
    if value is None:
        return "—"
    text = str(value).replace("\r\n", " ").replace("\n", " ").strip()
    return text.replace("|", "\\|") or "—"


def _fmt_seconds(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value)} s"
    except (TypeError, ValueError):
        return _md(value)


def _fmt_lufs(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value)} LUFS"
    except (TypeError, ValueError):
        return _md(value)


def _fmt_cost(value: Any) -> str:
    if value is None:
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return _md(value)
    # Sub-cent precision is load-bearing: real per-video LLM costs are
    # routinely $0.002–$0.06, and a flat ``:.2f`` renders them "$0.00" —
    # one of the Round 3 report/ledger contradictions.  Costs are
    # ledger-rounded to 6 dp upstream, so print up to 6 dp and trim
    # only trailing zeros (keeping at least cents).
    text = f"{amount:.6f}".rstrip("0")
    if text.endswith("."):
        text += "00"
    elif len(text.rsplit(".", 1)[-1]) == 1:
        text += "0"
    return f"${text}"


def _verdict(passed: Any) -> str:
    return "PASS" if passed else "FAIL"


def _render_generic(lines: list[str], label: str, value: Any) -> None:
    """Render an optional nested QC detail value under ``label``."""
    if value is None or value == "" or value == {} or value == []:
        return
    if isinstance(value, dict):
        for key in sorted(value, key=str):
            lines.append(f"- **{_md(key)}:** {_md(value[key])}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            lines.append(f"- {_md(item)}")
    else:
        lines.append(f"- **{label}:** {_md(value)}")


def write_report_md(result_data: dict, path: str | Path) -> Path:
    """Render the production report Markdown to ``path``; return it.

    ``result_data`` is a :class:`ProductionResult`-shaped dict (extra
    keys are ignored, except ``qc`` / ``qc_summary`` / ``qc_details``
    which, when present, are rendered inside the QC summary).
    """
    data = _as_result_dict(result_data)
    target = Path(path)

    title = data.get("title") or "Production Report"
    lines: list[str] = [f"# {_md(title)}", ""]

    # -- overview ---------------------------------------------------------
    lines += [
        "## Overview",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Project ID | {_md(data.get('project_id'))} |",
        f"| Niche | {_md(data.get('niche_id') or data.get('niche'))} |",
        f"| Topic | {_md(data.get('topic'))} |",
        f"| Title | {_md(title)} |",
        f"| Duration | {_fmt_seconds(data.get('duration_s'))} |",
        f"| Loudness | {_fmt_lufs(data.get('loudness_lufs'))} |",
        f"| Cost | {_fmt_cost(data.get('cost_usd'))} |",
        f"| Elapsed | {_fmt_seconds(data.get('elapsed_s'))} |",
        f"| Publish state | {_md(data.get('publish_state'))} |",
        "",
    ]

    # -- paths used ---------------------------------------------------------
    lines += ["## Paths Used", ""]
    paths_used = data.get("paths_used") or {}
    if isinstance(paths_used, dict) and paths_used:
        lines += ["| Stage | Path / Provider |", "|---|---|"]
        for stage in sorted(paths_used, key=str):
            lines.append(f"| {_md(stage)} | {_md(paths_used[stage])} |")
        lines.append("")
    else:
        lines += ["_No paths recorded._", ""]

    # -- gates -------------------------------------------------------------
    # Gate B carries its shared verdict label (Round 6 closure, W7):
    # a conditional release renders as "PASS — conditional_pass
    # [codes]" and a block keeps its typed reason codes — never a
    # bare PASS/FAIL the publish finaliser would contradict.
    _gate_b_cell = _verdict(data.get("gate_b_passed"))
    _gate_b_label = data.get("gate_b_verdict")
    if _gate_b_label and str(_gate_b_label) != "pass":
        _gate_b_cell += f" — {_md(_gate_b_label)}"
        _gate_b_codes = data.get("gate_b_reason_codes") or []
        if _gate_b_codes:
            _gate_b_cell += (
                " [" + ", ".join(str(c) for c in _gate_b_codes) + "]")
    lines += [
        "## Gates",
        "",
        "| Gate | Verdict |",
        "|---|---|",
        f"| Gate A (Script Lock) | {_verdict(data.get('gate_a_passed'))} |",
        f"| Gate B (Pre-Publish) | {_gate_b_cell} |",
        "",
    ]

    # -- LLM usage (ledger-derived; see app.integration.report_facts) -------
    llm_usage = data.get("llm_usage") or {}
    llm_models = data.get("llm_models") or []
    if llm_models or llm_usage or data.get("llm_usage_json"):
        usage = llm_usage if isinstance(llm_usage, dict) else {}
        lines += [
            "## LLM Usage",
            "",
            f"- **LLM path:** {_md((data.get('paths_used') or {}).get('llm'))}",
            f"- **LLM models:** {_md(', '.join(str(m) for m in llm_models) if llm_models else '—')}",
            f"- **LLM input tokens:** {_md(usage.get('input_tokens'))}",
            f"- **LLM output tokens:** {_md(usage.get('output_tokens'))}",
            f"- **LLM calls:** {_md(usage.get('calls'))}",
            f"- **LLM cost (USD):** {_fmt_cost(data.get('cost_usd'))}",
        ]
        if data.get("llm_usage_json"):
            lines.append(f"- **LLM usage ledger (JSON):** `{_md(data.get('llm_usage_json'))}`")
        lines.append("")

    # -- visuals sources (ledger-derived) -----------------------------------
    visual_sources = data.get("visual_sources") or []
    if visual_sources:
        lines += ["## Visuals Sources", ""]
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

    # -- QC summary ----------------------------------------------------------
    lines += [
        "## QC Summary",
        "",
        f"- **QC passed:** {_verdict(data.get('qc_passed'))}",
        f"- **Duration:** {_fmt_seconds(data.get('duration_s'))}",
        f"- **Loudness:** {_fmt_lufs(data.get('loudness_lufs'))}",
        f"- **Publish state:** {_md(data.get('publish_state'))}",
    ]
    for key in ("qc", "qc_summary", "qc_details"):
        if key in data:
            _render_generic(lines, key, data.get(key))
    lines.append("")

    # -- files ---------------------------------------------------------------
    lines += ["## Files", ""]
    files: list[tuple[str, Any]] = [
        ("Pack directory", data.get("pack_dir")),
        ("Final video", data.get("final_mp4")),
        ("Captions (SRT)", data.get("captions_srt")),
        ("Licence ledger (CSV)", data.get("licence_ledger_csv")),
        ("LLM usage ledger (JSON)", data.get("llm_usage_json")),
        ("Publish manifest (JSON)", data.get("publish_manifest_json")),
        ("Report (Markdown)", data.get("report_md") or str(target)),
    ]
    thumbnails = data.get("thumbnails") or []
    if isinstance(thumbnails, (list, tuple)):
        for index, thumbnail in enumerate(thumbnails, start=1):
            files.append((f"Thumbnail {index}", thumbnail))
    elif thumbnails:
        files.append(("Thumbnail", thumbnails))
    listed = [(label, value) for label, value in files if value]
    if listed:
        for label, value in listed:
            lines.append(f"- **{label}:** `{_md(value)}`")
    else:
        lines.append("_No files recorded._")
    lines.append("")

    # -- warnings --------------------------------------------------------------
    lines += ["## Warnings", ""]
    warnings = data.get("warnings") or []
    if isinstance(warnings, (list, tuple)) and warnings:
        for warning in warnings:
            lines.append(f"- {_md(warning)}")
    elif warnings:
        lines.append(f"- {_md(warnings)}")
    else:
        lines.append("None.")
    lines.append("")

    return _atomic_write_text(target, "\n".join(lines))
