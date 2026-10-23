"""Budget Guard — the deterministic cost-enforcement service.

Roadmap Part 10.4: a single service inside the Supervisor enforces six
nested cap levels. It is NOT an LLM agent; Agent 35 (Watchdog) only
monitors it. Other layers call::

    entry = await guard.reserve(...)   # before a provider call
    await guard.settle(entry.reservation_id, actual_cost_micros=...)
    await guard.release(entry.reservation_id)   # on abandonment

Invariants enforced here:
  * All money is integer micros; no float accumulation.
  * Reservations are atomic (one asyncio lock) — two parallel videos can
    never both assume the same last dollar.
  * Write-before-accept: settle()/release() failures PROPAGATE. A provider
    call whose cost cannot be recorded is treated as failed by callers.
  * NO SILENT OVERRIDES: the only cap-raising path is raise_cap(...,
    user_action=True), audit-logged. There is deliberately no agent /
    supervisor code path that raises a cap, switches a channel to a
    pricier stack, or enables a paid provider.
  * Threshold ladder per cap: 70% warning event -> 90% SOFT STOP (the
    downgrade ladder engages, logged, never silent) -> 100% HARD STOP
    (BudgetHardStop with spent/remaining/needed).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .downgrades import DOWNGRADE_LADDER, Downgrade
from .exceptions import (
    BudgetHardStop,
    InvalidLedgerState,
    L1LimitExceeded,
    NoSilentOverrideError,
    ReservationNotFound,
    RetryCapExceeded,
    RetryLimitExceeded,
)
from .ledger import InMemoryLedger, Ledger, LedgerEntry
from .money import usd_to_micros
from .quota import QuotaAccount

# L3 per-video HARD caps by stack (roadmap 10.4).
STACK_CAPS_MICROS: dict[str, int] = {
    "free": usd_to_micros("0.25"),
    "standard": usd_to_micros("1.00"),
    "premium": usd_to_micros("5.00"),
}

_AGENT_ACTORS = {"agent", "supervisor", "watchdog", "system", "llm", "orchestrator"}


@dataclass
class BudgetCaps:
    """Cap configuration — set by the USER at setup / in Settings."""

    stack: str = "standard"
    per_video_cap_micros: int | None = None  # None -> derived from stack
    videos_per_day: int = 2  # per channel, feeds the L4 derivation
    channel_daily_cap_micros: int | None = None  # None -> videos/day x video cap x 1.2
    global_daily_cap_micros: int = usd_to_micros("10")  # L5
    global_monthly_cap_micros: int = usd_to_micros("30")  # L6 (suggested starter)
    retry_subcap_pct: int = 15  # L2: retry+correction <= 15% of the video cap
    warn_pct: int = 70
    soft_pct: int = 90
    # L1 per-call limits
    max_output_tokens_per_call: int = 4000  # script call <= 4k out
    max_images_per_call: int = 10  # image batch <= 10
    max_tts_chars_per_call: int = 9000  # one full 10-min narration
    # L2 loop limits
    max_retries_per_step: int = 2
    max_correction_rounds: int = 2
    step_cap_micros: int | None = None  # None -> a step may use up to the video cap

    def video_cap(self, stack: str | None = None) -> int:
        if self.per_video_cap_micros is not None:
            return self.per_video_cap_micros
        return STACK_CAPS_MICROS[(stack or self.stack).lower()]


@dataclass(frozen=True)
class CallSpec:
    """What one provider call wants to do — checked against L1 pre-send."""

    output_tokens: int = 0
    images: int = 0
    tts_chars: int = 0


@dataclass
class ThresholdEvent:
    ts: datetime
    scope: str  # e.g. "video:v1", "channel_day:ch1:2026-10-23", "global_day:...", "global_month:..."
    level: str  # "warning" (70%) | "soft_stop" (90%) | "hard_stop" (100%)
    pct: float
    used_micros: int
    cap_micros: int


@dataclass
class AuditEntry:
    ts: datetime
    action: str  # "raise_cap"
    scope: str
    target: str
    old_cap_micros: int
    new_cap_micros: int
    actor: str
    reason: str = ""


@dataclass
class EstimateRecord:
    project_id: str
    estimate_micros: int
    catalog_version: str
    breakdown: dict = field(default_factory=dict)
    recorded_at: datetime | None = None
    actual_micros: int | None = None
    variance_pct: float | None = None
    flagged: bool = False


class BudgetGuard:
    def __init__(
        self,
        caps: BudgetCaps | None = None,
        ledger: Ledger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.caps = caps or BudgetCaps()
        self.ledger: Ledger = ledger or InMemoryLedger()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

        self.events: list[ThresholdEvent] = []
        self.audit_log: list[AuditEntry] = []
        self.project_log: list[dict] = []  # downgrades + cap raises (user-visible)
        self.downgrade_log: list[dict] = []

        self._fired: set[tuple[str, str]] = set()  # (scope, level) already emitted
        self._downgrades_active: dict[str, list[Downgrade]] = {}
        self._video_stack: dict[str, str] = {}
        self._video_cap_overrides: dict[str, int] = {}
        self._stack_cap_overrides: dict[str, int] = {}
        self._channel_cap_overrides: dict[str, int] = {}
        self._step_counters: dict[tuple[str, str], dict[str, int]] = {}
        self._estimates: dict[str, EstimateRecord] = {}
        self._estimate_history: list[EstimateRecord] = []
        self._quota_accounts: dict[str, QuotaAccount] = {}
        self._quota_by_channel: dict[str, str] = {}

    # ------------------------------------------------------------ internals
    def _get_lock(self) -> asyncio.Lock:
        # Lazily (re)bind per event loop so one Guard can serve sequential
        # asyncio.run() calls; within a loop all mutations stay atomic.
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _now(self) -> datetime:
        return self._clock()

    # ------------------------------------------------------------ cap maths
    def video_cap_micros(self, video_id: str, stack: str | None = None) -> int:
        if video_id in self._video_cap_overrides:
            return self._video_cap_overrides[video_id]
        resolved = stack or self._video_stack.get(video_id) or self.caps.stack
        if resolved.lower() in self._stack_cap_overrides:
            return self._stack_cap_overrides[resolved.lower()]
        return self.caps.video_cap(resolved)

    def channel_daily_cap_micros(self, channel_id: str, video_id: str | None = None) -> int:
        if channel_id in self._channel_cap_overrides:
            return self._channel_cap_overrides[channel_id]
        if self.caps.channel_daily_cap_micros is not None:
            return self.caps.channel_daily_cap_micros
        # L4 = videos/day x per-video cap x 1.2 headroom (integer-exact).
        video_cap = self.video_cap_micros(video_id or "", None) if video_id else self.caps.video_cap()
        return (self.caps.videos_per_day * video_cap * 12) // 10

    def retry_cap_micros(self, video_id: str) -> int:
        return (self.video_cap_micros(video_id) * self.caps.retry_subcap_pct) // 100

    # ------------------------------------------------------------ usage sums
    def _committed(self, predicate) -> int:
        return sum(e.committed_micros for e in self.ledger.entries() if predicate(e))

    def video_used_micros(self, video_id: str) -> int:
        return self._committed(lambda e: e.video_id == video_id)

    def video_remaining_micros(self, video_id: str) -> int:
        return max(0, self.video_cap_micros(video_id) - self.video_used_micros(video_id))

    def channel_day_used_micros(self, channel_id: str, on: datetime | None = None) -> int:
        day = (on or self._now()).date()
        return self._committed(lambda e: e.channel_id == channel_id and e.ts.date() == day)

    def global_day_used_micros(self, on: datetime | None = None) -> int:
        day = (on or self._now()).date()
        return self._committed(lambda e: e.ts.date() == day)

    def global_month_used_micros(self, on: datetime | None = None) -> int:
        ref = on or self._now()
        return self._committed(lambda e: e.ts.year == ref.year and e.ts.month == ref.month)

    def retry_used_micros(self, video_id: str) -> int:
        return self._committed(
            lambda e: e.video_id == video_id and e.category in ("retry", "correction")
        )

    # ---------------------------------------------------------- L1 per-call
    def validate_call_spec(self, spec: CallSpec) -> list[str]:
        violations: list[str] = []
        if spec.output_tokens > self.caps.max_output_tokens_per_call:
            violations.append(
                f"output_tokens {spec.output_tokens} > L1 max {self.caps.max_output_tokens_per_call}"
            )
        if spec.images > self.caps.max_images_per_call:
            violations.append(f"images {spec.images} > L1 max {self.caps.max_images_per_call}")
        if spec.tts_chars > self.caps.max_tts_chars_per_call:
            violations.append(
                f"tts_chars {spec.tts_chars} > L1 max {self.caps.max_tts_chars_per_call}"
            )
        return violations

    def truncate_call_spec(self, spec: CallSpec) -> CallSpec:
        """Opt-in L1 truncation: clamp a spec to the per-call limits."""
        return CallSpec(
            output_tokens=min(spec.output_tokens, self.caps.max_output_tokens_per_call),
            images=min(spec.images, self.caps.max_images_per_call),
            tts_chars=min(spec.tts_chars, self.caps.max_tts_chars_per_call),
        )

    # ------------------------------------------------------- threshold ladder
    def _scopes_for(self, entry_like_video: str, channel_id: str, ts: datetime) -> list[tuple[str, int, int]]:
        """(scope_key, used, cap) for L3..L6 given current ledger state."""
        day = ts.date().isoformat()
        month = f"{ts.year:04d}-{ts.month:02d}"
        return [
            (f"video:{entry_like_video}", self.video_used_micros(entry_like_video), self.video_cap_micros(entry_like_video)),
            (f"channel_day:{channel_id}:{day}", self.channel_day_used_micros(channel_id, ts), self.channel_daily_cap_micros(channel_id, entry_like_video)),
            (f"global_day:{day}", self.global_day_used_micros(ts), self.caps.global_daily_cap_micros),
            (f"global_month:{month}", self.global_month_used_micros(ts), self.caps.global_monthly_cap_micros),
        ]

    def _evaluate_thresholds(self, video_id: str, channel_id: str) -> None:
        """Fire newly crossed 70/90/100% levels, ascending, once per scope."""
        ts = self._now()
        for scope, used, cap in self._scopes_for(video_id, channel_id, ts):
            if cap <= 0:
                continue
            pct = used * 100.0 / cap
            for threshold, level in (
                (self.caps.warn_pct, "warning"),
                (self.caps.soft_pct, "soft_stop"),
                (100, "hard_stop"),
            ):
                key = (scope, level)
                if pct >= threshold and key not in self._fired:
                    self._fired.add(key)
                    self.events.append(
                        ThresholdEvent(ts=ts, scope=scope, level=level, pct=pct, used_micros=used, cap_micros=cap)
                    )
                    if level == "soft_stop":
                        self._engage_downgrades(scope, reason=f"{scope} reached {pct:.0f}% of cap")

    def _engage_downgrades(self, scope_key: str, *, reason: str) -> None:
        if scope_key in self._downgrades_active:
            return
        active = list(DOWNGRADE_LADDER)
        self._downgrades_active[scope_key] = active
        for step in active:
            record = {
                "ts": self._now().isoformat(),
                "type": "downgrade",
                "scope": scope_key,
                "downgrade_id": step.id,
                "order": step.order,
                "title": step.title,
                "reason": reason,
            }
            self.downgrade_log.append(record)
            self.project_log.append(record)  # never silent: project log + user report

    def active_downgrades(self, scope: str | None = None) -> list[Downgrade]:
        """Downgrades currently in force, in ladder order (① -> ⑤).

        ``scope`` may be a video_id, a channel_id, or None (any/global).
        """
        collected: dict[str, Downgrade] = {}
        for key, steps in self._downgrades_active.items():
            if scope is None or key == scope or key.startswith(f"video:{scope}") or key.startswith(f"channel_day:{scope}"):
                for step in steps:
                    collected[step.id] = step
        return sorted(collected.values(), key=lambda d: d.order)

    # ---------------------------------------------------------------- quotas
    def register_quota_account(self, account: QuotaAccount, *, channel_id: str | None = None) -> QuotaAccount:
        key = f"{account.provider}:{account.plan_name}"
        self._quota_accounts[key] = account
        if channel_id:
            self._quota_by_channel[channel_id] = key
        return account

    def quota_account(self, provider: str, plan_name: str) -> QuotaAccount | None:
        return self._quota_accounts.get(f"{provider}:{plan_name}")

    def quota_flagged_channels(self) -> list[str]:
        """Channels whose quota is projected to exhaust before its reset."""
        flagged: list[str] = []
        for channel_id, key in self._quota_by_channel.items():
            account = self._quota_accounts.get(key)
            if account is not None and account.flagged_for_downgrade():
                flagged.append(channel_id)
        return flagged

    # ------------------------------------------------- auto-mode (L6 effect)
    @property
    def auto_mode_enabled(self) -> bool:
        """L6 at 100% turns auto-mode OFF (manual-approve-only)."""
        return self.global_month_used_micros() < self.caps.global_monthly_cap_micros

    @property
    def manual_approve_only(self) -> bool:
        return not self.auto_mode_enabled

    # -------------------------------------------------------------- estimate
    def record_estimate(
        self,
        project_id: str,
        estimate_micros: int,
        catalog_version: str,
        breakdown: dict | None = None,
    ) -> EstimateRecord:
        record = EstimateRecord(
            project_id=project_id,
            estimate_micros=int(estimate_micros),
            catalog_version=catalog_version,
            breakdown=dict(breakdown or {}),
            recorded_at=self._now(),
        )
        self._estimates[project_id] = record
        return record

    def complete_project(self, project_id: str, actual_micros: int | None = None) -> EstimateRecord:
        record = self._estimates.get(project_id)
        if record is None:
            raise KeyError(f"no estimate recorded for project {project_id!r}")
        if actual_micros is None:
            actual_micros = sum(
                e.cost_micros
                for e in self.ledger.entries()
                if e.project_id == project_id and e.status == "settled"
            )
        record.actual_micros = int(actual_micros)
        if record.estimate_micros:
            record.variance_pct = round(
                (record.actual_micros - record.estimate_micros) * 100.0 / record.estimate_micros, 2
            )
            record.flagged = abs(record.variance_pct) > 25
        self._estimate_history.append(record)
        return record

    def variance_report(self, last_n: int = 10) -> dict:
        """Estimate-vs-actual over the last N completed projects.

        Variance beyond ±25% flags recalibration of the workload model.
        """
        history = self._estimate_history[-last_n:]
        entries = [
            {
                "project_id": r.project_id,
                "estimate_micros": r.estimate_micros,
                "actual_micros": r.actual_micros,
                "variance_pct": r.variance_pct,
                "catalog_version": r.catalog_version,
                "flagged": r.flagged,
            }
            for r in history
        ]
        variances = [r.variance_pct for r in history if r.variance_pct is not None]
        mean = round(sum(variances) / len(variances), 2) if variances else 0.0
        mean_abs = round(sum(abs(v) for v in variances) / len(variances), 2) if variances else 0.0
        return {
            "count": len(entries),
            "entries": entries,
            "mean_variance_pct": mean,
            "mean_abs_variance_pct": mean_abs,
            "needs_recalibration": mean_abs > 25,
            "flagged_project_ids": [r.project_id for r in history if r.flagged],
        }

    # ----------------------------------------------------------- raise a cap
    def raise_cap(
        self,
        scope: str,
        new_cap_micros: int,
        *,
        user_action: bool = False,
        actor: str = "user",
        reason: str = "",
        stack: str | None = None,
        video_id: str | None = None,
        channel_id: str | None = None,
    ) -> AuditEntry:
        """The ONLY way any cap moves up — an explicit, audited user action.

        Raises NoSilentOverrideError when the user-action marker is absent
        or the actor is an agent/supervisor/system identity.
        """
        if not user_action:
            raise NoSilentOverrideError(
                "raise_cap requires the explicit user-action marker (user_action=True)"
            )
        if actor.strip().lower() in _AGENT_ACTORS:
            raise NoSilentOverrideError(
                f"actor {actor!r} may never raise a cap — only the user can"
            )
        new_cap_micros = int(new_cap_micros)

        if scope == "global_day":
            old, target = self.caps.global_daily_cap_micros, "global_day"
            if new_cap_micros <= old:
                raise ValueError("new cap must be greater than the current cap")
            self.caps.global_daily_cap_micros = new_cap_micros
        elif scope == "global_month":
            old, target = self.caps.global_monthly_cap_micros, "global_month"
            if new_cap_micros <= old:
                raise ValueError("new cap must be greater than the current cap")
            self.caps.global_monthly_cap_micros = new_cap_micros
        elif scope == "channel_day":
            if not channel_id:
                raise ValueError("channel_day raise requires channel_id")
            old = self.channel_daily_cap_micros(channel_id)
            target = f"channel:{channel_id}"
            if new_cap_micros <= old:
                raise ValueError("new cap must be greater than the current cap")
            self._channel_cap_overrides[channel_id] = new_cap_micros
        elif scope in ("per_video", "video"):
            if video_id:
                old = self.video_cap_micros(video_id)
                target = f"video:{video_id}"
                if new_cap_micros <= old:
                    raise ValueError("new cap must be greater than the current cap")
                self._video_cap_overrides[video_id] = new_cap_micros
            elif stack:
                old = self.caps.video_cap(stack)
                target = f"stack:{stack.lower()}"
                if new_cap_micros <= old:
                    raise ValueError("new cap must be greater than the current cap")
                self._stack_cap_overrides[stack.lower()] = new_cap_micros
            else:
                raise ValueError("per_video raise requires video_id or stack")
        else:
            raise ValueError(f"unknown cap scope {scope!r}")

        entry = AuditEntry(
            ts=self._now(),
            action="raise_cap",
            scope=scope,
            target=target,
            old_cap_micros=old,
            new_cap_micros=new_cap_micros,
            actor=actor,
            reason=reason,
        )
        self.audit_log.append(entry)
        self.project_log.append(
            {
                "ts": entry.ts.isoformat(),
                "type": "raise_cap",
                "scope": scope,
                "target": target,
                "old_cap_micros": old,
                "new_cap_micros": new_cap_micros,
                "actor": actor,
                "reason": reason,
            }
        )
        return entry

    # ------------------------------------------------------ reserve / settle
    async def reserve(
        self,
        *,
        project_id: str,
        video_id: str,
        channel_id: str,
        agent_id: str,
        step: str,
        provider: str,
        model: str,
        operation: str,
        estimated_cost_micros: int,
        category: str = "primary",
        units_in: int = 0,
        units_out: int = 0,
        units_cached: int = 0,
        unit_price_snapshot: str = "",
        quota_units: int = 0,
        quota_account_key: str | None = None,
        call_spec: CallSpec | None = None,
        stack: str | None = None,
        manual_approved: bool = False,
    ) -> LedgerEntry:
        """Reserve an estimate against EVERY applicable cap (L1-L6), atomically.

        Raises L1LimitExceeded (call never sent), RetryLimitExceeded /
        RetryCapExceeded (L2), or BudgetHardStop (L3-L6 breach).
        """
        estimated_cost_micros = int(estimated_cost_micros)
        if estimated_cost_micros < 0:
            raise ValueError("estimated_cost_micros cannot be negative")

        # L1 — cheapest place to stop waste: reject BEFORE sending.
        if call_spec is not None:
            violations = self.validate_call_spec(call_spec)
            if violations:
                raise L1LimitExceeded(violations)

        async with self._get_lock():
            if stack:
                self._video_stack[video_id] = stack.lower()
            ts = self._now()

            # L2 — attempt / correction-round limits per step.
            counters = self._step_counters.setdefault((video_id, step), {"retry": 0, "correction": 0})
            if category == "retry" and counters["retry"] >= self.caps.max_retries_per_step:
                raise RetryLimitExceeded(
                    f"step {step!r} on video {video_id!r}: max {self.caps.max_retries_per_step} retries used"
                )
            if category == "correction" and counters["correction"] >= self.caps.max_correction_rounds:
                raise RetryLimitExceeded(
                    f"step {step!r} on video {video_id!r}: max {self.caps.max_correction_rounds} correction rounds used"
                )
            # L2 — step budget share.
            step_cap = self.caps.step_cap_micros if self.caps.step_cap_micros is not None else self.video_cap_micros(video_id)
            if estimated_cost_micros > step_cap:
                raise BudgetHardStop(
                    scope=f"step:{step}",
                    cap_micros=step_cap,
                    spent_micros=0,
                    requested_micros=estimated_cost_micros,
                    video_id=video_id,
                    channel_id=channel_id,
                    message=f"L2 step budget exceeded: step {step!r} estimate {estimated_cost_micros} > step cap {step_cap}",
                )
            # L2 — retry sub-cap (<=15% of the video cap), tracked separately.
            if category in ("retry", "correction"):
                retry_cap = self.retry_cap_micros(video_id)
                retry_used = self.retry_used_micros(video_id)
                if retry_used + estimated_cost_micros > retry_cap:
                    raise RetryCapExceeded(
                        f"retry sub-cap exceeded for video {video_id!r}: "
                        f"retry spend {retry_used} + {estimated_cost_micros} > {retry_cap} "
                        f"({self.caps.retry_subcap_pct}% of video cap)"
                    )

            # L3-L6 — prospective totals against every applicable cap.
            checks = [
                ("video", self.video_used_micros(video_id), self.video_cap_micros(video_id)),
                ("channel_day", self.channel_day_used_micros(channel_id, ts), self.channel_daily_cap_micros(channel_id, video_id)),
                ("global_day", self.global_day_used_micros(ts), self.caps.global_daily_cap_micros),
                ("global_month", self.global_month_used_micros(ts), self.caps.global_monthly_cap_micros),
            ]
            for scope_name, used, cap in checks:
                if scope_name == "global_month" and manual_approved:
                    continue  # L6 at 100% = manual-approve-only, not a wall
                if used + estimated_cost_micros > cap:
                    raise BudgetHardStop(
                        scope=scope_name,
                        cap_micros=cap,
                        spent_micros=used,
                        requested_micros=estimated_cost_micros,
                        video_id=video_id,
                        channel_id=channel_id,
                    )

            entry = LedgerEntry(
                ts=ts,
                project_id=project_id,
                video_id=video_id,
                channel_id=channel_id,
                agent_id=agent_id,
                step=step,
                provider=provider,
                model=model,
                operation=operation,
                cost_micros=estimated_cost_micros,
                category=category,
                status="reserved",
                units_in=units_in,
                units_out=units_out,
                units_cached=units_cached,
                unit_price_snapshot=unit_price_snapshot,
                quota_units_spent=0,  # quota burns on settle, at actuals
            )
            self.ledger.append(entry)
            if category in counters:
                counters[category] += 1
            if quota_account_key:
                self._pending_quota(entry.reservation_id, quota_account_key, quota_units)
            self._evaluate_thresholds(video_id, channel_id)
            return entry

    # Quota intent attached to a reservation; burned at settle time.
    def _pending_quota(self, reservation_id: str, account_key: str, units: int) -> None:
        self.__dict__.setdefault("_quota_intent", {})[reservation_id] = (account_key, int(units))

    async def settle(
        self,
        reservation_id: str,
        *,
        actual_cost_micros: int,
        units_in: int | None = None,
        units_out: int | None = None,
        units_cached: int | None = None,
        quota_units: int | None = None,
    ) -> LedgerEntry:
        """Settle a reservation at ACTUAL cost; the remainder is released.

        Write-before-accept: any failure here (unknown id, wrong state,
        quota exhaustion) RAISES and propagates — the caller must treat the
        provider call as failed. Over-cap actuals are still recorded
        truthfully (the money is spent) and trip the threshold ladder.
        """
        async with self._get_lock():
            entry = self.ledger.get(reservation_id)
            if entry is None:
                raise ReservationNotFound(f"unknown reservation {reservation_id!r}")
            if entry.status != "reserved":
                raise InvalidLedgerState(
                    f"reservation {reservation_id!r} is {entry.status!r}, cannot settle"
                )
            # Quota burn first: if it cannot be recorded, nothing settles.
            intent = self.__dict__.get("_quota_intent", {}).get(reservation_id)
            burn_units = quota_units if quota_units is not None else (intent[1] if intent else 0)
            if intent and burn_units:
                account = self._quota_accounts.get(intent[0])
                if account is None:
                    raise ReservationNotFound(f"quota account {intent[0]!r} not registered")
                account.burn(burn_units, product="narration")
                entry.quota_units_spent = burn_units

            entry.cost_micros = int(actual_cost_micros)
            entry.status = "settled"
            entry.settled_at = self._now()
            if units_in is not None:
                entry.units_in = units_in
            if units_out is not None:
                entry.units_out = units_out
            if units_cached is not None:
                entry.units_cached = units_cached
            self._evaluate_thresholds(entry.video_id, entry.channel_id)
            return entry

    async def release(self, reservation_id: str, *, reason: str = "abandoned") -> LedgerEntry:
        """Release a reservation in full (abandonment / step never ran)."""
        async with self._get_lock():
            entry = self.ledger.get(reservation_id)
            if entry is None:
                raise ReservationNotFound(f"unknown reservation {reservation_id!r}")
            if entry.status != "reserved":
                raise InvalidLedgerState(
                    f"reservation {reservation_id!r} is {entry.status!r}, cannot release"
                )
            entry.status = "released"
            entry.released_at = self._now()
            entry.release_reason = reason
            return entry

    # Abandonment alias.
    abandon = release
