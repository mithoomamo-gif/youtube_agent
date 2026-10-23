"""Reporting (Agent 31) — stored reports: receipts + weekly digests.

Service-layer contract (shared with the sibling report-engine work):

* ``generate_report(scope, channel_id, video_id=None, period=None,
  kind='receipt')`` -> the stored artefact dict (markdown + JSON).
* ``list_reports(channel_id=None, scope=None, kind=None)`` ->
  artefact summaries, newest first.
* ``get_report(report_id, channel_id=None)`` -> full artefact or
  ``None`` (``None`` also when the report lives under a different
  channel — existence never leaks across channels).

Every figure in every report is derived at generation time from the
ledgers of record (``projects``, ``api_usage``, ``publish_records``,
``analytics_snapshots``, ``jobs`` / ``agent_runs`` / ``schedule_runs``,
``schedules``) — never invented, never carried in parallel counters.

Receipts are produced by W1's engine (:mod:`app.reporting`) behind
this contract; the weekly digest is built here
(:mod:`app.reports.digest`).  See :mod:`app.reports.service`.
"""

from __future__ import annotations

from app.reports.service import generate_report, get_report, list_reports

__all__ = ["generate_report", "get_report", "list_reports"]
