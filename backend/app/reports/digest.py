"""Weekly channel digest (Agent 31) — a period variant of the receipt.

Aggregates ONE week for ONE channel, from the ledgers only:

1. Production — videos created in the week, by status.
2. Publishing — publish records in the week; fake-provider
   simulations are labelled as simulations, never as live uploads.
3. Costs — ``api_usage`` actuals for the channel's projects.
4. Quota — YouTube units used in the week (shared Google-project
   budget, labelled as such).
5. Performance — deltas between the first and last
   ``analytics_snapshots`` of the week per video; when the channel
   has no snapshots the section says "no analytics yet" instead of
   printing invented zeros.
6. Failures & recoveries — failed/dead jobs, failed agent runs,
   non-created schedule runs, and projects that failed but recovered.
7. Gate decisions — summary counts from ``gate_status_json``.
8. Planned next week — enabled schedules + the occurrences the
   scheduler itself computes for the coming week.

Freshness honesty (roadmap §3.15 / Phase 7): the digest states its
window and a "data through" date; analytics numbers are snapshots,
never "live".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.reports import facts
from app.reports.facts import WEEK_MS


def _day(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _iso(ms: Any) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _usd(micros: Any) -> str:
    return f"${int(micros or 0) / 1_000_000:.4f}"


def build_digest_data(conn, channel: dict, period: dict) -> dict[str, Any]:
    start_ms, end_ms = period["start_ms"], period["end_ms"]
    channel_id = channel["id"]
    projects = facts.projects_created_in_window(conn, channel_id, start_ms, end_ms)
    project_ids = [p["id"] for p in projects]
    failures = facts.failures_in_window(conn, channel_id, start_ms, end_ms)
    by_status: dict[str, int] = {}
    for project in projects:
        status = str(project.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "scope": "channel",
        "kind": "weekly_digest",
        "channel": {"id": channel_id, "title": channel.get("title")},
        "period": period,
        "production": {
            "total_created": len(projects),
            "by_status": by_status,
            "videos": [
                {
                    "project_id": p["id"],
                    "topic": p.get("topic"),
                    "status": p.get("status"),
                    "created_at": p.get("created_at"),
                    "total_cost_micros": int(p.get("total_cost_micros") or 0),
                }
                for p in projects
            ],
        },
        "publishing": [
            {
                "project_id": r["project_id"],
                "topic": r.get("project_topic"),
                "youtube_video_id": r.get("youtube_video_id"),
                "simulated": bool(r.get("simulated")),
                "privacy_status": r.get("privacy_status"),
                "created_at": r.get("created_at"),
            }
            for r in facts.publishes_in_window(conn, channel_id, start_ms, end_ms)
        ],
        "costs": facts.usage_rollup(conn, project_ids, start_ms, end_ms),
        "project_costs_total_micros": sum(
            int(p.get("total_cost_micros") or 0) for p in projects
        ),
        "quota": facts.quota_used_in_window(conn, start_ms, end_ms),
        "performance": facts.analytics_deltas(conn, channel_id, start_ms, end_ms),
        "failures": failures,
        "recoveries": facts.recoveries_in_window(
            conn, channel_id, start_ms, end_ms, failures["failed_jobs"]
        ),
        "gates": facts.gate_summary(conn, projects),
        "planned_next_week": facts.planned_next_week(
            conn, channel_id, end_ms, end_ms + WEEK_MS
        ),
    }


def render_digest(data: dict[str, Any]) -> tuple[str, str]:
    period = data["period"]
    start_ms, end_ms = period["start_ms"], period["end_ms"]
    title = (
        f"Weekly digest — {data['channel'].get('title')}"
        f" — {_day(start_ms)} to {_day(end_ms)}"
    )
    lines = [
        f"# {title}",
        "",
        f"7-day snapshot, data through {_day(end_ms)}"
        f" (window {_iso(start_ms)} → {_iso(end_ms)})."
        " All figures come from the app's ledgers; analytics are"
        " snapshots, not live numbers.",
        "",
        "## Production",
        "",
    ]
    production = data["production"]
    if production["total_created"]:
        summary = ", ".join(
            f"{count} {status}"
            for status, count in sorted(production["by_status"].items())
        )
        lines.append(f"{production['total_created']} video(s) created this week: {summary}.")
        lines.append("")
        for video in production["videos"]:
            lines.append(
                f"- {video.get('topic')} — {video.get('status')},"
                f" cost {_usd(video.get('total_cost_micros'))} (`{video['project_id']}`)"
            )
    else:
        lines.append("No videos were created this week.")
    lines += ["", "## Publishing", ""]
    if data["publishing"]:
        for pub in data["publishing"]:
            if pub["simulated"]:
                lines.append(
                    f"- {pub.get('topic')} — SIMULATED upload (fake provider,"
                    f" not live on YouTube): `{pub.get('youtube_video_id')}`,"
                    f" privacy {pub.get('privacy_status')}."
                )
            else:
                lines.append(
                    f"- {pub.get('topic')} — published record:"
                    f" `{pub.get('youtube_video_id')}`,"
                    f" privacy {pub.get('privacy_status')}."
                )
    else:
        lines.append("Nothing was published this week.")
    lines += ["", "## Costs (actuals)", ""]
    costs = data["costs"]
    if costs["providers"]:
        lines.append("| Provider | Calls | Tokens in | Tokens out | Characters | Images | Cost |")
        lines.append("|---|---|---|---|---|---|---|")
        for row in costs["providers"]:
            lines.append(
                f"| {row['provider']} | {row['calls']} | {row['tokens_in']}"
                f" | {row['tokens_out']} | {row['characters']} | {row['images']}"
                f" | {_usd(row['cost_micros'])} |"
            )
        lines.append("")
    lines.append(
        f"Provider ledger total for this week's videos:"
        f" **{_usd(costs['total_cost_micros'])}** across {costs['total_calls']} calls."
        f" Sum of the projects' cumulative cost counters:"
        f" {_usd(data['project_costs_total_micros'])}."
    )
    lines += ["", "## YouTube quota used", ""]
    quota = data["quota"]
    buckets = quota["buckets"]
    limits = quota.get("limits") or {}
    lines.append(
        "Quota is a shared Google-project budget (not per channel):"
        f" uploads {buckets['uploads']}"
        + (f"/{limits['uploads']}" if "uploads" in limits else "")
        + f", search {buckets['search']}"
        + (f"/{limits['search']}" if "search" in limits else "")
        + f", shared {buckets['shared']}"
        + (f"/{limits['shared']}" if "shared" in limits else "")
        + f" — {quota['total_units']} units total this week."
    )
    lines += ["", "## Performance", ""]
    performance = data["performance"]
    if not performance["present"]:
        lines.append(
            "No analytics yet — no analytics snapshots were recorded for"
            " this channel this week, so there are no performance"
            " figures to report."
        )
    else:
        totals = performance["totals"]
        lines.append(
            f"Across {len(performance['videos'])} video(s) with snapshots:"
            f" views {totals['views']:+d}, watch hours {totals['watch_hours']:+.2f},"
            f" likes {totals['likes']:+d}, comments {totals['comments_count']:+d},"
            f" subscribers {totals['subs_delta']:+d} (first→last snapshot deltas)."
        )
        lines.append("")
        for video in performance["videos"]:
            lines.append(
                f"- `{video.get('youtube_video_id') or video.get('project_id')}`:"
                f" views {video['views_delta']:+d} (latest {video['views_latest']}),"
                f" watch hours {video['watch_hours_delta']:+.2f},"
                f" subs {video['subs_delta']:+d},"
                f" {video['snapshots']} snapshot(s),"
                f" last at {_iso(video.get('last_snapshot_at'))}."
            )
    lines += ["", "## Failures & recoveries", ""]
    failures = data["failures"]
    failure_lines: list[str] = []
    for job in failures["failed_jobs"]:
        failure_lines.append(
            f"- Job `{job['id']}` ({job.get('agent_key')}, project"
            f" `{job.get('project_id')}`) ended {job.get('status')}"
            f" after {job.get('attempts')} attempt(s):"
            f" {job.get('error_code') or ''} {job.get('error') or ''}".rstrip()
        )
    for run in failures["failed_agent_runs"]:
        failure_lines.append(
            f"- Agent run ({run.get('agent_key')}, project"
            f" `{run.get('project_id')}`) {run.get('status')}:"
            f" {run.get('error') or ''}".rstrip()
        )
    for run in failures["schedule_runs"]:
        failure_lines.append(
            f"- Schedule run ({run.get('schedule_name')}) {run.get('status')}:"
            f" {run.get('reason') or ''}".rstrip()
        )
    if failure_lines:
        lines.extend(failure_lines)
    else:
        lines.append("No failures recorded this week.")
    if data["recoveries"]:
        lines.append("")
        lines.append("Recovered (failed during the week, no longer failed):")
        for recovery in data["recoveries"]:
            lines.append(
                f"- {recovery.get('topic')} (`{recovery['project_id']}`)"
                f" — now {recovery.get('current_status')}."
            )
    lines += ["", "## Gate decisions", ""]
    if data["gates"]:
        for gate, counts in sorted(data["gates"].items()):
            rendered = ", ".join(
                f"{count} {label}" for label, count in sorted(counts.items())
            )
            lines.append(f"- {gate}: {rendered}")
    else:
        lines.append("No gate decisions recorded on this week's videos.")
    lines += ["", "## Planned next week", ""]
    planned = data["planned_next_week"]
    if planned:
        for item in planned:
            occurrences = item["occurrences_in_horizon"]
            if not item["enabled"]:
                lines.append(f"- {item.get('name')} — disabled, nothing planned.")
            elif occurrences:
                rendered = ", ".join(_iso(ts) for ts in occurrences)
                lines.append(
                    f"- {item.get('name')} ({item.get('cadence_type') or 'custom'}"
                    f" cadence, {item.get('topic_mode')} topics):"
                    f" {len(occurrences)} run(s) — {rendered}."
                )
            else:
                lines.append(
                    f"- {item.get('name')} — enabled, no occurrences in the"
                    " coming week."
                )
    else:
        lines.append("No schedules exist for this channel — nothing is planned.")
    return title, "\n".join(lines) + "\n"
