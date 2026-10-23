"""Publish-window suggestion service (suggestions ONLY).

Implements the roadmap Auto (Analytics) eligibility rule
(MASTER_ROADMAP §3.10): a channel is eligible only at
**≥10 published videos AND ≥28 days of analytics data**.  Below
either threshold the service returns the honest structured result
``{eligible: False, reason: 'not_enough_data', published_count,
days_of_data, required: {videos: 10, days: 28}}`` and fabricates
**no** window.

When eligible, the channel's own published videos are bucketed by
publish **hour in the channel's stored timezone** (never UTC,
never server time; weekday is the secondary view) and ranked by
mean first-24h velocity.  Every suggested window carries its
evidence — contributing video count, mean / min / max velocity and
spread (population standard deviation) — and hours backed by
fewer than :data:`LOW_EVIDENCE_MIN_VIDEOS` videos are flagged
``low_evidence`` instead of being silently trusted (a single lucky
video can top a mean ranking; the flag is how that stays honest).

Suggestion, never application
-----------------------------
This service computes proposals.  It **never** writes to or
modifies any schedule (no schedule table is read or written
anywhere in this package), and it never activates a learning
rule.  When a Learning Store is supplied and the channel is
eligible, the top suggestion is recorded via the store's
``create_candidate`` as a ``publish_window`` rule in status
``candidate`` — promotion to ``active`` is a separate explicit
step owned elsewhere, per the store's evidence rules.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.publish_window.adapter import (
    VELOCITY_METRIC,
    ChannelPublishData,
    PublishedVideoVelocity,
    PublishWindowSource,
)

__all__ = [
    "LOW_EVIDENCE_MIN_VIDEOS",
    "MIN_ANALYTICS_DAYS",
    "MIN_PUBLISHED_VIDEOS",
    "RULE_KIND",
    "STATEMENT_FAMILY",
    "PublishWindowService",
]

#: Roadmap eligibility thresholds (MASTER_ROADMAP §3.10).
MIN_PUBLISHED_VIDEOS = 10
MIN_ANALYTICS_DAYS = 28

#: An hour/weekday bucket backed by fewer videos than this is
#: flagged ``low_evidence`` in the suggestion.
LOW_EVIDENCE_MIN_VIDEOS = 3

#: Learning Store kind / statement family for these suggestions.
RULE_KIND = "publish_window"
STATEMENT_FAMILY = "publish_window"

_DAY_MS = 86_400_000
_WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


def _days_of_data(data: ChannelPublishData) -> int:
    """Whole days spanned by the channel's analytics snapshots."""
    if data.analytics_first_ms is None or data.analytics_last_ms is None:
        return 0
    span_ms = int(data.analytics_last_ms) - int(data.analytics_first_ms)
    if span_ms < 0:
        return 0
    return span_ms // _DAY_MS


def _bucket_stats(velocities: list[float]) -> dict[str, Any]:
    return {
        "video_count": len(velocities),
        "mean_velocity": round(statistics.fmean(velocities), 3),
        "min_velocity": min(velocities),
        "max_velocity": max(velocities),
        # Spread: population standard deviation of the contributing
        # videos' first-24h velocities (0.0 for a single video).
        "spread": round(statistics.pstdev(velocities), 3)
        if len(velocities) > 1 else 0.0,
        "low_evidence": len(velocities) < LOW_EVIDENCE_MIN_VIDEOS,
    }


def _aggregate(
    videos: list[PublishedVideoVelocity], tz: ZoneInfo
) -> tuple[list[dict], list[dict]]:
    """(hour_windows, weekday_windows), each ranked by mean velocity.

    Ranking: mean first-24h velocity desc, then contributing video
    count desc, then bucket key asc — fully deterministic.
    """
    by_hour: dict[int, list[tuple[str, float]]] = {}
    by_weekday: dict[int, list[tuple[str, float]]] = {}
    for video in videos:
        local = datetime.fromtimestamp(
            video.published_at_ms / 1000, tz=timezone.utc
        ).astimezone(tz)
        velocity = float(video.first_24h_velocity)  # not None: filtered
        by_hour.setdefault(local.hour, []).append((video.video_id, velocity))
        by_weekday.setdefault(local.weekday(), []).append(
            (video.video_id, velocity)
        )

    hour_windows = []
    for hour, entries in by_hour.items():
        stats = _bucket_stats([v for _, v in entries])
        hour_windows.append({
            "start_hour": hour,
            "end_hour": (hour + 1) % 24,
            "label": f"{hour:02d}:00–{(hour + 1) % 24:02d}:00",
            **stats,
            "video_ids": [vid for vid, _ in entries],
        })
    hour_windows.sort(
        key=lambda w: (-w["mean_velocity"], -w["video_count"], w["start_hour"])
    )
    for rank, window in enumerate(hour_windows, start=1):
        window["rank"] = rank

    weekday_windows = []
    for weekday, entries in by_weekday.items():
        stats = _bucket_stats([v for _, v in entries])
        weekday_windows.append({
            "weekday": weekday,  # 0 = Monday, in channel-local time
            "weekday_name": _WEEKDAY_NAMES[weekday],
            **stats,
            "video_ids": [vid for vid, _ in entries],
        })
    weekday_windows.sort(
        key=lambda w: (-w["mean_velocity"], -w["video_count"], w["weekday"])
    )
    for rank, window in enumerate(weekday_windows, start=1):
        window["rank"] = rank
    return hour_windows, weekday_windows


class PublishWindowService:
    """Computes publish-window suggestions for one channel at a time.

    ``source`` is any :class:`PublishWindowSource`.  ``learning_store``
    is optional: when given, it must expose the landed Learning Store
    signature ``create_candidate(channel_id, niche, kind, statement,
    *, statement_family=..., effect_value=..., effect_unit=...,
    evidence_count=..., confidence=..., created_from=...)`` returning
    the stored rule dict (:class:`app.learning.store.LearningStore`).
    """

    def __init__(
        self,
        source: PublishWindowSource,
        learning_store: Optional[Any] = None,
    ) -> None:
        self._source = source
        self._learning_store = learning_store

    # ------------------------------------------------------------------
    def suggest(
        self,
        channel_id: str,
        *,
        niche: Optional[str] = None,
        persist: bool = True,
        top_n: int = 3,
    ) -> dict[str, Any]:
        """Return the publish-window suggestion result for a channel.

        Not-eligible shape (exact, per the brief)::

            {eligible: False, reason: 'not_enough_data',
             published_count, days_of_data,
             required: {videos: 10, days: 28}, ...}

        with ``suggestions == []`` and no learning rule written.
        Eligible results carry ranked ``suggestions`` (top ``top_n``
        hour windows), the full ``hour_evidence`` / ``weekday_windows``
        aggregations, and — when persisted — the candidate
        ``learning_rule`` created for the top window.
        """
        channel_id = str(channel_id)
        data = self._source.fetch_channel(channel_id)
        published_count = len(data.videos)
        days_of_data = _days_of_data(data)
        base: dict[str, Any] = {
            "kind": RULE_KIND,
            "channel_id": channel_id,
            "timezone": data.timezone_name,
            "timezone_source": data.timezone_source,
            "velocity_metric": VELOCITY_METRIC,
            "published_count": published_count,
            "days_of_data": days_of_data,
            "required": {
                "videos": MIN_PUBLISHED_VIDEOS,
                "days": MIN_ANALYTICS_DAYS,
            },
            "suggestions": [],
            "learning_rule": None,
        }
        if not data.exists:
            return {**base, "eligible": False, "reason": "channel_not_found"}
        if (
            published_count < MIN_PUBLISHED_VIDEOS
            or days_of_data < MIN_ANALYTICS_DAYS
        ):
            return {**base, "eligible": False, "reason": "not_enough_data"}

        with_velocity = [
            v for v in data.videos if v.first_24h_velocity is not None
        ]
        tz = ZoneInfo(data.timezone_name)
        hour_windows, weekday_windows = _aggregate(with_velocity, tz)
        suggestions = hour_windows[: max(0, int(top_n))]

        result: dict[str, Any] = {
            **base,
            "eligible": True,
            "reason": None,
            "videos_with_velocity": len(with_velocity),
            "videos_without_velocity": published_count - len(with_velocity),
            "suggestions": suggestions,
            "hour_evidence": hour_windows,
            "weekday_windows": weekday_windows,
        }
        if persist and suggestions:
            result["learning_rule"] = self._persist_candidate(
                result, data, niche=niche, top=suggestions[0],
                total_velocity_videos=len(with_velocity),
            )
        return result

    # ------------------------------------------------------------------
    def _persist_candidate(
        self,
        result: dict[str, Any],
        data: ChannelPublishData,
        *,
        niche: Optional[str],
        top: dict[str, Any],
        total_velocity_videos: int,
    ) -> Optional[dict]:
        """Store the top suggestion as a CANDIDATE learning rule.

        Never activates.  Returns the stored rule dict, or ``None``
        (with the reason recorded on ``result``) when no store is
        configured, the channel's niche scope is unknown, or the
        store rejects the write — the suggestion itself is still
        returned as data in every case.
        """
        if self._learning_store is None:
            result["learning_rule_note"] = (
                "no_learning_store_configured: suggestion returned as "
                "data only"
            )
            return None
        scope_niche = niche or data.niche
        if not scope_niche:
            result["learning_rule_note"] = (
                "niche_scope_unknown: channel has no default niche, "
                "so no learning rule was stored"
            )
            return None
        statement = (
            f"Publish in the {top['label']} window ({data.timezone_name}): "
            f"highest mean first-24h velocity "
            f"({top['mean_velocity']:.1f} views) across "
            f"{top['video_count']} of this channel's published videos."
        )
        confidence = (
            round(top["video_count"] / total_velocity_videos, 3)
            if total_velocity_videos else None
        )
        try:
            rule = self._learning_store.create_candidate(
                data.channel_id,
                scope_niche,
                RULE_KIND,
                statement,
                statement_family=STATEMENT_FAMILY,
                effect_value=float(top["mean_velocity"]),
                effect_unit="mean_views_first_24h",
                evidence_count=int(top["video_count"]),
                confidence=confidence,
                created_from=list(top["video_ids"]),
            )
        except Exception as exc:  # store failure must not lose the suggestion
            result["learning_rule_note"] = (
                f"learning_store_write_failed: {exc!r}"
            )
            return None
        result["learning_rule_note"] = (
            "stored as candidate via LearningStore.create_candidate; "
            "activation is a separate explicit step"
        )
        return rule if isinstance(rule, dict) else {"raw": rule}
