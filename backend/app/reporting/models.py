"""Report data model — the JSON document is the source of truth.

One shared model backs BOTH report outputs (roadmap Agent 31,
MASTER_ROADMAP §3.15 / Phase 7): the engine builds a
:class:`ReportDocument`, the JSON rendering is
``document.model_dump()``, and the Markdown rendering
(:mod:`app.reporting.render`) is a pure function of that same dict.
Markdown never carries a figure that is not in the JSON.

Honesty invariants baked into the model:

* Every figure comes from a ledger / record table (see
  :mod:`app.reporting.engine` for the per-section source map).  The
  model has no computed-at-render-time fields.
* ``publish.classification`` is one of ``published`` /
  ``simulated`` / ``not_published`` / ``unknown``.  A fake-provider
  simulation is ALWAYS ``simulated`` and its label says so — it can
  never be rendered as "published".
* ``performance.available`` is False with an explicit reason when no
  ``analytics_snapshots`` rows exist ("no analytics yet"); the
  metrics fields then stay at their empty defaults, never invented.
* ``calibration_error.available`` is False with an explicit reason
  when the learning team has provided no calibration error for the
  scope (see the seam in :mod:`app.reporting.engine`).
* Money is integer ``*_micros`` (USD x 1,000,000) with a float
  ``*_usd`` mirror, per the project money convention.  Cost totals
  are reported PER LEDGER (project ledger / api_usage ledger /
  agent-runs ledger / budget reservations) and never summed across
  ledgers — the ledgers overlap by design, so a cross-ledger sum
  would double-count.

Sections are ``extra="allow"`` so the engine (and later Round 5
workers) can add ledger-derived fields without breaking stored
reports or the W2 HTTP layer; the fields declared here are the
stable contract W2 can rely on.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "SCHEMA_VERSION",
    "Period",
    "ChannelRef",
    "VideoRef",
    "ProductionSection",
    "PublishSection",
    "CostsSection",
    "QuotaSection",
    "PerformanceSection",
    "GatesSection",
    "FailuresSection",
    "CalibrationErrorSection",
    "NextPlannedSection",
    "ReportDocument",
]

#: Report JSON schema version (``report_json`` payloads carry it).
SCHEMA_VERSION = "1.0"


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow")


class Period(_Section):
    """The window a report covers.

    ``start_ms`` / ``end_ms`` are epoch milliseconds; both NULL means
    the report covers all recorded time (``label == "all_time"`` for
    channel reports, ``"lifetime"`` for a video's own receipt).
    """

    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    label: str = "all_time"


class ChannelRef(_Section):
    id: str
    title: Optional[str] = None
    handle: Optional[str] = None
    youtube_channel_id: Optional[str] = None
    language: Optional[str] = None
    status: Optional[str] = None


class VideoRef(_Section):
    """The video a ``scope="video"`` report is the receipt for."""

    project_id: str
    topic: Optional[str] = None
    title: Optional[str] = None  # publish-record title, when one exists
    status: Optional[str] = None
    current_phase: Optional[str] = None
    niche_profile_id: Optional[str] = None
    topic_source: Optional[str] = None
    target_minutes: Optional[float] = None
    target_words: Optional[int] = None
    publish_mode: Optional[str] = None
    schedule_id: Optional[str] = None
    youtube_video_id: Optional[str] = None
    created_at_ms: Optional[int] = None
    updated_at_ms: Optional[int] = None


class ProductionSection(_Section):
    """What was produced — from projects / artefacts / asset ledgers."""

    #: Video scope: number of artefacts recorded for the project.
    #: Channel scope: number of projects (videos) in the period.
    videos_produced: int = 0
    projects_by_status: dict[str, int] = Field(default_factory=dict)
    artefacts: list[dict[str, Any]] = Field(default_factory=list)
    artefact_count: int = 0
    assets: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    #: Channel scope only: one summary dict per video in the period.
    videos: list[dict[str, Any]] = Field(default_factory=list)


class PublishSection(_Section):
    """Publish state, honestly classified.

    ``classification``:
    * ``published`` — a publish record exists whose video id is NOT a
      simulated id and the project itself is in ``published`` state;
    * ``simulated`` — the only publish evidence is the fake provider
      (``simulated:``-namespaced id, ``gate_status_json.publish``
      with ``simulated=True``, or state
      ``fake_provider_upload_simulated``).  Labelled simulated in
      every rendering, never "published";
    * ``not_published`` — no publish record / explicit non-publication;
    * ``unknown`` — a publish record exists but its provenance cannot
      be classified from the ledgers (reported as-is, never upgraded
      to "published").
    """

    classification: str = "not_published"
    label: str = "Not published"
    simulated: bool = False
    provider: Optional[str] = None
    publish_record_id: Optional[str] = None
    youtube_video_id: Optional[str] = None
    video_url: Optional[str] = None
    privacy_status: Optional[str] = None
    published_at_ms: Optional[int] = None
    state: Optional[str] = None
    detail: Optional[str] = None
    #: Channel scope: counts + per-video classifications.
    counts: dict[str, int] = Field(default_factory=dict)


class CostsSection(_Section):
    """Production cost actuals, per ledger (never cross-summed)."""

    #: Headline actual: sum of ``projects.total_cost_micros`` in scope.
    total_micros: int = 0
    total_usd: float = 0.0
    project_ledger_micros: int = 0
    api_usage_micros: int = 0
    agent_runs_micros: int = 0
    api_usage_calls: int = 0
    by_provider: list[dict[str, Any]] = Field(default_factory=list)
    by_operation: list[dict[str, Any]] = Field(default_factory=list)
    by_agent: list[dict[str, Any]] = Field(default_factory=list)
    by_project: list[dict[str, Any]] = Field(default_factory=list)
    reservations: list[dict[str, Any]] = Field(default_factory=list)
    reserved_micros: int = 0
    settled_micros: int = 0
    source_note: str = (
        "Totals are reported per ledger (projects.total_cost_micros, "
        "api_usage.cost_micros, agent_runs.cost_micros, budget "
        "reservations) and are never summed across ledgers, because "
        "the ledgers overlap by design."
    )


class QuotaSection(_Section):
    """YouTube quota used — from ``api_usage`` rows with
    ``provider='youtube'`` only, bucketed by the quota module's own
    ``classify_method`` so the report can never drift from the
    quota view / enforcement accounting."""

    total_units: int = 0
    calls: int = 0
    buckets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    by_operation: list[dict[str, Any]] = Field(default_factory=list)
    source_note: str = (
        "Units are the stored api_usage.units values; rows with NULL "
        "units fall back to the per-method cost from "
        "app.providers.youtube.quota.classify_method."
    )


class PerformanceSection(_Section):
    """Performance from ``analytics_snapshots`` only."""

    available: bool = False
    reason: Optional[str] = "no analytics yet"
    snapshot_count: int = 0
    videos_with_analytics: int = 0
    latest: Optional[dict[str, Any]] = None
    totals_from_latest_per_video: dict[str, Any] = Field(default_factory=dict)
    snapshots: list[dict[str, Any]] = Field(default_factory=list)


class GatesSection(_Section):
    """Gate A / B / C decisions.

    Per-gate outcome is normalised to ``passed`` / ``failed`` /
    ``blocked`` / ``pending`` / ``not_recorded`` plus the raw ledger
    value it came from.  A gate with no ledger evidence is
    ``not_recorded`` — never assumed passed.
    """

    gate_a: dict[str, Any] = Field(
        default_factory=lambda: {"outcome": "not_recorded", "raw": None}
    )
    gate_b: dict[str, Any] = Field(
        default_factory=lambda: {"outcome": "not_recorded", "raw": None}
    )
    gate_c: dict[str, Any] = Field(
        default_factory=lambda: {"outcome": "not_recorded", "raw": None}
    )
    #: Channel scope: outcome counts across the period's videos.
    counts: dict[str, Any] = Field(default_factory=dict)


class FailuresSection(_Section):
    """Failures + recoveries during production.

    A *recovery* is ledger evidence that work which failed later
    succeeded: a job with ``attempts > 1`` whose final status is
    ``done``, or an agent that has both a failed and a later
    succeeded run for the same project.
    """

    failure_count: int = 0
    recovery_count: int = 0
    failures: list[dict[str, Any]] = Field(default_factory=list)
    recoveries: list[dict[str, Any]] = Field(default_factory=list)
    failed_jobs: int = 0
    failed_agent_runs: int = 0
    failed_schedule_runs: int = 0


class CalibrationErrorSection(_Section):
    """Calibration error from the learning team, when provided.

    ``available`` False is the honest default: the field renders as
    "not available" and no number is invented.  See
    :func:`app.reporting.engine.load_calibration_error` for the seams
    consumed (the landed ``app.calibration`` read model over the
    ``calibration_log`` ledger first of all).  When the source is
    ``app.calibration``, ``value`` stays None and the per-(niche,
    metric, snapshot-point) errors live in ``detail['scopes']`` —
    the calibration team's math pools no errors across metrics, so
    neither does this report.
    """

    available: bool = False
    reason: Optional[str] = (
        "not available — no calibration error has been provided "
        "for this scope yet"
    )
    source: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    detail: Optional[dict[str, Any]] = None


class NextPlannedSection(_Section):
    """Next planned work — from ``schedules`` + ``topic_queue``."""

    schedules: list[dict[str, Any]] = Field(default_factory=list)
    enabled_schedule_count: int = 0
    next_run_at_ms: Optional[int] = None
    pending_topics: list[dict[str, Any]] = Field(default_factory=list)
    pending_topic_count: int = 0
    recent_schedule_runs: list[dict[str, Any]] = Field(default_factory=list)


class ReportDocument(_Section):
    """One generated report (the JSON source of truth)."""

    schema_version: str = SCHEMA_VERSION
    report_id: str
    scope: str  # "video" | "channel"
    channel_id: str
    project_id: Optional[str] = None
    channel: Optional[ChannelRef] = None
    video: Optional[VideoRef] = None
    title: str = "Report"
    generated_at_ms: int
    generated_at: str = ""  # ISO-8601 UTC rendering of generated_at_ms
    period: Period = Field(default_factory=Period)
    production: ProductionSection = Field(default_factory=ProductionSection)
    publish: PublishSection = Field(default_factory=PublishSection)
    costs: CostsSection = Field(default_factory=CostsSection)
    quota: QuotaSection = Field(default_factory=QuotaSection)
    performance: PerformanceSection = Field(default_factory=PerformanceSection)
    gates: GatesSection = Field(default_factory=GatesSection)
    failures_recoveries: FailuresSection = Field(default_factory=FailuresSection)
    calibration_error: CalibrationErrorSection = Field(
        default_factory=CalibrationErrorSection
    )
    next_planned: NextPlannedSection = Field(default_factory=NextPlannedSection)
    #: Ledger tables this report read from (provenance for the reader).
    sources: list[str] = Field(default_factory=list)
    honesty_notes: list[str] = Field(default_factory=list)
