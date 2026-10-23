"""Exceptions for the Budget Guard.

The Guard is a DETERMINISTIC service: every refusal is a typed exception,
never an LLM judgement call and never a silent clamp (except the explicit
L1 truncation helper, which the caller opts into).
"""
from __future__ import annotations


class BudgetError(Exception):
    """Base class for all Budget Guard errors."""


class ReservationRefused(BudgetError):
    """A reservation was refused because it would breach an applicable cap."""


class BudgetHardStop(ReservationRefused):
    """100% of a cap reached — the DAG must pause at its next gate.

    Carries the exact figures the UI / report needs: what was spent, what
    remains under the cap, and the exact additional amount needed to fund
    the refused call (`needed_micros` = shortfall beyond `remaining_micros`).
    """

    def __init__(
        self,
        *,
        scope: str,
        cap_micros: int,
        spent_micros: int,
        requested_micros: int,
        video_id: str | None = None,
        channel_id: str | None = None,
        message: str | None = None,
    ) -> None:
        self.scope = scope
        self.cap_micros = int(cap_micros)
        self.spent_micros = int(spent_micros)
        self.requested_micros = int(requested_micros)
        self.remaining_micros = max(0, self.cap_micros - self.spent_micros)
        # Exact amount needed *beyond what remains* to fund the refused call.
        self.needed_micros = max(0, self.requested_micros - self.remaining_micros)
        self.video_id = video_id
        self.channel_id = channel_id
        super().__init__(
            message
            or (
                f"Budget hard stop [{scope}]: spent {self.spent_micros} micros of "
                f"{self.cap_micros} cap; remaining {self.remaining_micros}; "
                f"refused call requested {self.requested_micros} "
                f"(needs {self.needed_micros} more)"
            )
        )

    @property
    def payload(self) -> dict:
        """Serializable spent/remaining/needed figures for UI + reports."""
        return {
            "scope": self.scope,
            "cap_micros": self.cap_micros,
            "spent_micros": self.spent_micros,
            "remaining_micros": self.remaining_micros,
            "requested_micros": self.requested_micros,
            "needed_micros": self.needed_micros,
            "video_id": self.video_id,
            "channel_id": self.channel_id,
        }


class RetryCapExceeded(ReservationRefused):
    """Retry/correction spend would exceed the L2 sub-cap (<=15% of video cap)."""


class RetryLimitExceeded(ReservationRefused):
    """Max retry attempts or correction rounds for a step already used up."""


class L1LimitExceeded(BudgetError):
    """A single call spec exceeds its L1 per-call limit (rejected pre-send)."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = list(violations)
        super().__init__("L1 per-call limit exceeded: " + "; ".join(self.violations))


class NoSilentOverrideError(BudgetError, PermissionError):
    """A cap change was attempted without the explicit user-action marker.

    Invariant (roadmap 10.4.5): agents — including the Supervisor — can
    NEVER raise a cap, switch a channel to a pricier stack, or enable a
    paid provider. The only path is ``BudgetGuard.raise_cap(...,
    user_action=True)`` performed on behalf of a human in Settings.
    """


class LedgerWriteError(BudgetError):
    """A ledger write failed.

    Write-before-accept rule: a provider call whose cost cannot be recorded
    is treated as FAILED — callers must let this propagate, never swallow it.
    """


class ReservationNotFound(LedgerWriteError):
    """settle()/release() referenced an unknown reservation id."""


class InvalidLedgerState(LedgerWriteError):
    """settle()/release() on an entry that is not in `reserved` state."""


class QuotaExhausted(BudgetError):
    """A quota burn would exceed the subscription quota total."""
