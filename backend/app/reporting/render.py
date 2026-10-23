"""Markdown rendering for reports — a pure function of the JSON model.

The JSON document (:mod:`app.reporting.models`) is the source of
truth; this module renders it to the human-readable "receipt" the
roadmap asks Agent 31 for.  No figure is computed here: every number
printed comes from the dict it is handed, so the Markdown can never
diverge from the JSON (the Round 3 report-integrity rule, applied to
Round 5 reports).

Empty / missing data renders as an explicit honest section
("No analytics yet", "Not available", "None recorded") — never as a
fabricated zero dressed up as a measurement.  (True ledger zeros —
e.g. a quota total of 0 from an empty ``api_usage`` ledger — ARE
printed as 0, because the ledger says so.)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

__all__ = ["render_markdown", "render_report_markdown"]


def _fmt_ms(ms: Any) -> str:
    if ms is None:
        return "—"
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return str(ms)
    return (
        datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _fmt_usd(micros: Any) -> str:
    try:
        value = int(micros or 0)
    except (TypeError, ValueError):
        value = 0
    return f"${value / 1_000_000:.6f} ({value} micros)"


def _esc(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _get(data: Mapping[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, Mapping):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render one report dict (the JSON model) to Markdown."""
    scope = str(report.get("scope") or "")
    channel = report.get("channel") or {}
    video = report.get("video") or {}
    period = report.get("period") or {}
    lines: list[str] = []

    title = str(report.get("title") or "Report")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- **Report ID:** `{report.get('report_id', '')}`")
    lines.append(f"- **Scope:** {scope}")
    if channel:
        lines.append(
            f"- **Channel:** {_esc(channel.get('title') or channel.get('id') or '')}"
            f" (`{channel.get('id', '')}`)"
        )
    if scope == "video" and video:
        lines.append(f"- **Project:** `{video.get('project_id', '')}`")
        if video.get("topic"):
            lines.append(f"- **Topic:** {_esc(video.get('topic'))}")
    lines.append(
        f"- **Generated:** {_fmt_ms(report.get('generated_at_ms'))}"
    )
    period_label = period.get("label") or "all_time"
    lines.append(
        f"- **Period:** {period_label}"
        f" ({_fmt_ms(period.get('start_ms'))} → {_fmt_ms(period.get('end_ms'))})"
    )
    lines.append("")

    # -- Production ------------------------------------------------------
    prod = report.get("production") or {}
    lines.append("## What was produced")
    lines.append("")
    if scope == "channel":
        lines.append(f"- Videos (projects) in period: **{prod.get('videos_produced', 0)}**")
        by_status = prod.get("projects_by_status") or {}
        if by_status:
            for status in sorted(by_status):
                lines.append(f"  - {status}: {by_status[status]}")
        else:
            lines.append("  - No projects recorded in this period.")
        videos = prod.get("videos") or []
        if videos:
            lines.append("")
            lines.append("| Video (project) | Topic | Status | Cost (project ledger) | Publish |")
            lines.append("|---|---|---|---|---|")
            for v in videos:
                lines.append(
                    f"| `{_esc(v.get('project_id'))}` | {_esc(v.get('topic'))} "
                    f"| {_esc(v.get('status'))} | {_fmt_usd(v.get('cost_micros', 0))} "
                    f"| {_esc(v.get('publish_classification'))} |"
                )
    else:
        lines.append(f"- Project status: **{_esc(video.get('status') or 'unknown')}**")
        if video.get("current_phase"):
            lines.append(f"- Current phase: {_esc(video.get('current_phase'))}")
        lines.append(f"- Artefacts recorded: **{prod.get('artefact_count', 0)}**")
        artefacts = prod.get("artefacts") or []
        if artefacts:
            lines.append("")
            for art in artefacts:
                lines.append(
                    f"  - {_esc(art.get('artefact_type'))} v{art.get('version')}"
                    f" — `{_esc(art.get('file_path'))}`"
                    + (" (locked)" if art.get("is_locked") else "")
                )
        else:
            lines.append("  - No artefacts recorded for this project.")
        assets = prod.get("assets") or {}
        if assets:
            lines.append(
                f"- Assets used: **{assets.get('asset_count', 0)}**"
                f" (licence rows: {assets.get('licence_count', 0)})"
            )
            for row in assets.get("by_type") or []:
                lines.append(
                    f"  - {_esc(row.get('asset_type'))} via "
                    f"{_esc(row.get('source_provider'))}: {row.get('count')}"
                )
    lines.append("")

    # -- Publish -----------------------------------------------------------
    pub = report.get("publish") or {}
    lines.append("## Publish state")
    lines.append("")
    lines.append(f"- **{pub.get('label', 'Not published')}**"
                 f" (classification: `{pub.get('classification', 'not_published')}`)")
    if pub.get("simulated"):
        lines.append(
            "- ⚠️ This was a **simulated** publish via the fake provider. "
            "No video was uploaded to YouTube."
        )
    if pub.get("provider"):
        lines.append(f"- Provider: {_esc(pub.get('provider'))}")
    if pub.get("youtube_video_id"):
        lines.append(f"- Video ID (as recorded): `{_esc(pub.get('youtube_video_id'))}`")
    if pub.get("video_url"):
        lines.append(f"- Video URL: {_esc(pub.get('video_url'))}")
    if pub.get("privacy_status"):
        lines.append(f"- Privacy: {_esc(pub.get('privacy_status'))}")
    if pub.get("published_at_ms") is not None:
        lines.append(f"- Published at: {_fmt_ms(pub.get('published_at_ms'))}")
    if pub.get("detail"):
        lines.append(f"- Detail: {_esc(pub.get('detail'))}")
    counts = pub.get("counts") or {}
    if counts:
        lines.append("- Counts in period: "
                     + ", ".join(f"{k}: {counts[k]}" for k in sorted(counts)))
    lines.append("")

    # -- Costs -------------------------------------------------------------
    costs = report.get("costs") or {}
    lines.append("## Production cost (actuals, from ledgers)")
    lines.append("")
    lines.append(f"- **Total (project ledger): {_fmt_usd(costs.get('total_micros', 0))}**")
    lines.append(f"- api_usage ledger: {_fmt_usd(costs.get('api_usage_micros', 0))}"
                 f" across {costs.get('api_usage_calls', 0)} call(s)")
    lines.append(f"- agent_runs ledger: {_fmt_usd(costs.get('agent_runs_micros', 0))}")
    if costs.get("reservations"):
        lines.append(f"- Budget reservations: settled "
                     f"{_fmt_usd(costs.get('settled_micros', 0))}, reserved "
                     f"{_fmt_usd(costs.get('reserved_micros', 0))}")
    for row in costs.get("by_provider") or []:
        lines.append(
            f"  - {_esc(row.get('provider'))}: {_fmt_usd(row.get('cost_micros', 0))}"
            f" ({row.get('calls', 0)} calls)"
        )
    if not (costs.get("by_provider") or []):
        lines.append("  - No provider cost rows recorded.")
    if costs.get("source_note"):
        lines.append(f"- _{_esc(costs.get('source_note'))}_")
    lines.append("")

    # -- Quota -------------------------------------------------------------
    quota = report.get("quota") or {}
    lines.append("## YouTube quota used")
    lines.append("")
    lines.append(f"- Total units: **{quota.get('total_units', 0)}**"
                 f" across {quota.get('calls', 0)} recorded call(s)")
    buckets = quota.get("buckets") or {}
    for name in sorted(buckets):
        bucket = buckets[name] or {}
        limit = bucket.get("limit")
        suffix = f" / limit {limit}" if limit is not None else ""
        lines.append(f"  - {name}: {bucket.get('used', 0)}{suffix}")
    if not buckets:
        lines.append("  - No quota rows recorded.")
    lines.append("")

    # -- Performance ---------------------------------------------------------
    perf = report.get("performance") or {}
    lines.append("## Performance")
    lines.append("")
    if not perf.get("available"):
        lines.append(f"_No analytics yet — {_esc(perf.get('reason') or 'no analytics snapshots recorded')}._")
    else:
        lines.append(f"- Snapshots recorded: {perf.get('snapshot_count', 0)}")
        lines.append(f"- Videos with analytics: {perf.get('videos_with_analytics', 0)}")
        totals = perf.get("totals_from_latest_per_video") or {}
        if totals:
            lines.append(
                f"- Latest-per-video totals: views {totals.get('views', 0)}, "
                f"likes {totals.get('likes', 0)}, comments {totals.get('comments_count', 0)}, "
                f"watch hours {totals.get('watch_hours', 0)}"
            )
        latest = perf.get("latest") or {}
        if latest:
            lines.append(
                f"- Latest snapshot: {_fmt_ms(latest.get('snapshot_at_ms'))}"
                f" — views {latest.get('views', 0)}, likes {latest.get('likes', 0)},"
                f" comments {latest.get('comments_count', 0)}"
            )
    lines.append("")

    # -- Gates ---------------------------------------------------------------
    gates = report.get("gates") or {}
    lines.append("## Gate decisions")
    lines.append("")
    for key, label in (("gate_a", "Gate A — Script Lock"),
                       ("gate_b", "Gate B — Pre-Publish"),
                       ("gate_c", "Gate C — Publish")):
        gate = gates.get(key) or {}
        lines.append(f"- **{label}:** {gate.get('outcome', 'not_recorded')}"
                     + (f" (recorded as: {_esc(gate.get('raw'))})"
                        if gate.get("raw") is not None else ""))
        for reason in gate.get("reasons") or []:
            lines.append(f"  - {_esc(reason)}")
    gate_counts = gates.get("counts") or {}
    if gate_counts:
        lines.append(f"- Period counts: {_esc(gate_counts)}")
    lines.append("")

    # -- Failures & recoveries ----------------------------------------------
    fail = report.get("failures_recoveries") or {}
    lines.append("## Failures & recoveries")
    lines.append("")
    lines.append(f"- Failures recorded: **{fail.get('failure_count', 0)}**"
                 f" (jobs: {fail.get('failed_jobs', 0)}, agent runs: "
                 f"{fail.get('failed_agent_runs', 0)}, schedule runs: "
                 f"{fail.get('failed_schedule_runs', 0)})")
    lines.append(f"- Recoveries recorded: **{fail.get('recovery_count', 0)}**")
    for item in fail.get("failures") or []:
        lines.append(f"  - [{_esc(item.get('kind'))}] {_esc(item.get('label'))}"
                     f": {_esc(item.get('error') or item.get('status') or '')}")
    if not (fail.get("failures") or []):
        lines.append("  - None recorded.")
    for item in fail.get("recoveries") or []:
        lines.append(f"  - Recovered: [{_esc(item.get('kind'))}] {_esc(item.get('label'))}"
                     f" — {_esc(item.get('detail') or '')}")
    lines.append("")

    # -- Calibration -----------------------------------------------------------
    calib = report.get("calibration_error") or {}
    lines.append("## Calibration error")
    lines.append("")
    if not calib.get("available"):
        lines.append(f"_Not available — {_esc(calib.get('reason') or 'no calibration error provided')}._")
    elif calib.get("value") is not None:
        lines.append(f"- Value: {calib.get('value')} {_esc(calib.get('unit') or '')}"
                     f" (source: {_esc(calib.get('source') or 'learning')})")
    else:
        # Per-scope calibration (app.calibration): errors are reported
        # per (niche, metric, snapshot point) and never pooled.
        lines.append(f"- Source: {_esc(calib.get('source') or 'learning')}"
                     " — per-scope errors (never pooled across metrics):")
        detail = calib.get("detail") or {}
        for scope_info in detail.get("scopes") or []:
            lines.append(
                f"  - niche `{_esc(scope_info.get('niche'))}`, metric "
                f"`{_esc(scope_info.get('metric'))}` "
                f"({scope_info.get('entry_count', 0)} entries, "
                f"{scope_info.get('video_count', 0)} videos)")
            for point_name, point in sorted((scope_info.get("points") or {}).items()):
                if point.get("mape_pct") is None:
                    lines.append(
                        f"    - {point_name}: {_esc(point.get('label') or 'insufficient evidence')}"
                        f" (n={point.get('n', 0)})")
                else:
                    lines.append(
                        f"    - {point_name}: MAPE {point.get('mape_pct'):.2f}%, "
                        f"bias {point.get('signed_bias_pct'):.2f}% "
                        f"({_esc(point.get('bias_direction') or '')}), "
                        f"band coverage {point.get('band_coverage_pct'):.1f}% "
                        f"(n={point.get('n', 0)})")
        for row in detail.get("video_rows") or []:
            lines.append(
                f"  - This video ({_esc(row.get('snapshot_point'))}, "
                f"{_esc(row.get('metric'))}): predicted "
                f"{row.get('predicted_low')}–{row.get('predicted_high')}, "
                f"actual {row.get('actual_value')} — {_esc(row.get('verdict'))}")
    lines.append("")

    # -- Next planned ------------------------------------------------------------
    nxt = report.get("next_planned") or {}
    lines.append("## Next planned work")
    lines.append("")
    lines.append(f"- Enabled schedules: {nxt.get('enabled_schedule_count', 0)}")
    if nxt.get("next_run_at_ms") is not None:
        lines.append(f"- Next run: {_fmt_ms(nxt.get('next_run_at_ms'))}")
    for sched in nxt.get("schedules") or []:
        state = "enabled" if sched.get("is_enabled") else "disabled"
        lines.append(f"  - {_esc(sched.get('name'))} ({state})"
                     f" — next: {_fmt_ms(sched.get('next_run_at_ms'))}")
    lines.append(f"- Pending topics in queue: {nxt.get('pending_topic_count', 0)}")
    for topic in nxt.get("pending_topics") or []:
        lines.append(f"  - {_esc(topic.get('topic'))}")
    if not (nxt.get("schedules") or []) and not (nxt.get("pending_topics") or []):
        lines.append("  - Nothing planned in the ledgers.")
    lines.append("")

    # -- Honesty footer ------------------------------------------------------------
    lines.append("---")
    lines.append("")
    sources = report.get("sources") or []
    if sources:
        lines.append(f"_Sources (ledgers only): {', '.join(str(s) for s in sources)}._")
        lines.append("")
    for note in report.get("honesty_notes") or []:
        lines.append(f"_Note: {_esc(note)}_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# Alias matching the service-layer name W2 was briefed on.
render_report_markdown = render_markdown
