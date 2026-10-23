"""Reservation ledger — interface + in-memory implementation.

(SQLite persistence is another team's job; the Guard depends only on the
:class:`Ledger` interface below.)

Lifecycle: ``reserved`` -> ``settled`` (actual cost, remainder released) or
``reserved`` -> ``released`` (abandoned, full estimate freed).

Committed funds for cap purposes = settled actuals + live reservations.
Released entries commit nothing.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

CATEGORIES = ("primary", "retry", "correction", "downgrade_saving")
STATUSES = ("reserved", "settled", "released")


@dataclass
class LedgerEntry:
    ts: datetime
    project_id: str
    video_id: str
    channel_id: str
    agent_id: str
    step: str
    provider: str
    model: str
    operation: str
    cost_micros: int  # estimate while reserved; ACTUAL once settled
    category: str = "primary"  # primary | retry | correction | downgrade_saving
    status: str = "reserved"  # reserved | settled | released
    units_in: int = 0
    units_out: int = 0
    units_cached: int = 0
    unit_price_snapshot: str = ""  # catalog row reference, e.g. "openai/gpt-5:input_token[api_payg]@2026-10-22.1"
    quota_units_spent: int = 0
    reservation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    reserved_cost_micros: int = 0  # original estimate, kept after settle for variance
    settled_at: datetime | None = None
    released_at: datetime | None = None
    release_reason: str = ""

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown ledger category {self.category!r}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown ledger status {self.status!r}")
        if not self.reserved_cost_micros:
            self.reserved_cost_micros = self.cost_micros

    @property
    def id(self) -> str:  # convenient alias
        return self.reservation_id

    @property
    def committed_micros(self) -> int:
        """Funds this entry currently commits against caps."""
        if self.status == "released":
            return 0
        return self.cost_micros  # reserved: estimate; settled: actual


class Ledger(ABC):
    """Ledger interface the Budget Guard enforces against."""

    @abstractmethod
    def append(self, entry: LedgerEntry) -> LedgerEntry: ...

    @abstractmethod
    def get(self, reservation_id: str) -> LedgerEntry | None: ...

    @abstractmethod
    def entries(self) -> list[LedgerEntry]: ...


class InMemoryLedger(Ledger):
    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._by_id: dict[str, LedgerEntry] = {}

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        self._entries.append(entry)
        self._by_id[entry.reservation_id] = entry
        return entry

    def get(self, reservation_id: str) -> LedgerEntry | None:
        return self._by_id.get(reservation_id)

    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def for_video(self, video_id: str) -> list[LedgerEntry]:
        return [e for e in self._entries if e.video_id == video_id]

    def for_project(self, project_id: str) -> list[LedgerEntry]:
        return [e for e in self._entries if e.project_id == project_id]
