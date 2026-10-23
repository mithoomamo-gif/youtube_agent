"""Quota architecture — June 2026 granular buckets (roadmap Section 25).

Since 1 Jun 2026 a project gets THREE independent daily buckets, reset at
midnight Pacific Time:

- ``upload`` — 100 ``videos.insert`` calls/day, 1 unit each.
- ``search`` — 100 ``search.list`` calls/day, 1 unit each.
- ``shared`` — 10,000 units/day for everything else, at per-method costs
  (reads 1, ``thumbnails.set`` 50, ``captions.insert`` 400,
  ``playlists.create`` 50, ``playlistItems.insert`` 50,
  ``videos.update`` 50).

Multi-channel does NOT multiply quota — buckets are per *project*.

The ledger is the record of what Google *charged*. Failed/invalid calls
still cost >=1 unit, so entries are written for failed calls too. The
SQLite-backed ledger is another team's job; this module defines the
protocol plus an in-memory implementation. The pre-flight reservation /
safety-ceiling enforcement lives in :class:`~.client.YouTubeClient`,
which is the ONLY path API calls may take.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

BUCKET_UPLOAD = "upload"
BUCKET_SEARCH = "search"
BUCKET_SHARED = "shared"

#: Daily limit per bucket (units; for upload/search 1 unit == 1 call).
BUCKET_LIMITS: dict[str, int] = {
    BUCKET_UPLOAD: 100,
    BUCKET_SEARCH: 100,
    BUCKET_SHARED: 10_000,
}

#: Default fraction of each bucket the app is allowed to plan against.
#: The remaining 10% is headroom for retries and interactive UI actions.
DEFAULT_SAFETY_CEILING = 0.9

#: Explicit per-method costs in the shared bucket. Anything not listed
#: here (and not videos.insert / search.list) is a read-class call at 1.
_SHARED_COSTS: dict[str, int] = {
    "thumbnails.set": 50,
    "captions.insert": 400,
    "captions.list": 50,
    "captions.update": 450,
    "captions.delete": 50,
    "playlists.create": 50,
    "playlists.insert": 50,
    "playlistitems.insert": 50,
    "videos.update": 50,
    "comments.insert": 50,
}


def normalize_method(method: str) -> str:
    """Normalize a method name for cost lookup.

    Accepts ``videos.insert``, ``youtube.videos.insert`` or a bare
    resource path segment; comparison is case-insensitive so
    ``playlistItems.insert`` matches.
    """
    parts = [p for p in method.strip().split(".") if p]
    if len(parts) > 2:
        parts = parts[-2:]
    return ".".join(parts).lower()


def classify_method(method: str) -> tuple[str, int]:
    """Return ``(bucket, unit_cost)`` for an API method.

    - ``videos.insert`` -> upload bucket, 1 unit.
    - ``search.list`` -> search bucket, 1 unit.
    - everything else -> shared bucket at its listed cost (default 1).
    """
    normalized = normalize_method(method)
    if normalized == "videos.insert":
        return BUCKET_UPLOAD, 1
    if normalized == "search.list":
        return BUCKET_SEARCH, 1
    return BUCKET_SHARED, _SHARED_COSTS.get(normalized, 1)


def pacific_day(now: datetime | None = None) -> date:
    """The current calendar day in America/Los_Angeles."""
    if now is None:
        now = datetime.now(PACIFIC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=PACIFIC)
    return now.astimezone(PACIFIC).date()


def next_reset(now: datetime | None = None) -> datetime:
    """The next bucket reset instant: midnight Pacific Time tonight."""
    today = pacific_day(now)
    return datetime.combine(today + timedelta(days=1), time.min, tzinfo=PACIFIC)


@dataclass
class LedgerEntry:
    """One charged API call."""

    bucket: str
    method: str
    units: int
    day_pt: date = field(default_factory=pacific_day)
    channel_id: str | None = None
    project_id: str | None = None
    job_id: str | None = None
    success: bool = True
    called_at: datetime = field(default_factory=lambda: datetime.now(PACIFIC))


@runtime_checkable
class QuotaLedger(Protocol):
    """Append-only record of charged quota (SQLite impl lives elsewhere)."""

    def record(self, entry: LedgerEntry) -> None:
        """Append one entry. Called for successful AND failed calls."""
        ...

    def used_units(self, bucket: str, day: date) -> int:
        """Total units charged against ``bucket`` on Pacific day ``day``."""
        ...

    def entries_for(self, day: date) -> list[LedgerEntry]:
        """All entries recorded for Pacific day ``day``."""
        ...


class InMemoryQuotaLedger:
    """List-backed :class:`QuotaLedger` for tests and local dev."""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def record(self, entry: LedgerEntry) -> None:
        self._entries.append(entry)

    def used_units(self, bucket: str, day: date) -> int:
        return sum(
            e.units for e in self._entries if e.bucket == bucket and e.day_pt == day
        )

    def entries_for(self, day: date) -> list[LedgerEntry]:
        return [e for e in self._entries if e.day_pt == day]

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)
