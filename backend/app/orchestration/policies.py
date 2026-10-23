"""Retry / correction / escalation policies for the Supervisor graph.

Source of truth: roadmap section 08 (Reliability) and Part 5.6:

* Input/quality failures (fact-check, QC, copyright, policy FAILs) are
  NEVER blind-retried.  They are re-routed as correction work to the
  responsible upstream agent, with a maximum of **2 correction rounds**;
  after that the task is BLOCKED and escalated to the user.
* Transient failures use bounded retries with backoff — never unbounded
  spend recovering one video.
* Escalations are plain language: what happened, in words a non-engineer
  can act on (full technical detail lives in the dead-letter record).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: Maximum number of correction rounds for a quality/input failure before
#: the Supervisor stops re-routing and escalates (roadmap 5.6 / section 08).
MAX_CORRECTION_ROUNDS: int = 2


def next_correction_action(rounds_used: int) -> Literal["retry", "escalate"]:
    """Decide what happens after a correction round has been used.

    ``rounds_used`` is the number of correction rounds already consumed for
    the failing step.  While fewer than ``MAX_CORRECTION_ROUNDS`` have been
    used, one more correction round (``"retry"``) is allowed; once the cap
    is reached the only honest action is ``"escalate"``.
    """
    if rounds_used < MAX_CORRECTION_ROUNDS:
        return "retry"
    return "escalate"


@dataclass
class RetryPolicy:
    """Bounded retry policy for transient failures.

    Defaults mirror the supervisor brief: at most 3 attempts, waiting
    2s, then 8s, then 30s between them.  (Section 08's longer production
    schedule for network-class errors is configured per provider; this
    dataclass is the in-graph default and is deliberately finite.)
    """

    max_attempts: int = 3
    backoff_seconds: list[float] = field(default_factory=lambda: [2, 8, 30])

    def backoff_for_attempt(self, attempt: int) -> float:
        """Backoff (seconds) to wait after the 0-based ``attempt`` fails.

        Attempts beyond the configured list reuse the last (largest)
        delay, so the schedule never raises or returns ``None``.
        """
        if not self.backoff_seconds:
            return 0.0
        index = min(max(attempt, 0), len(self.backoff_seconds) - 1)
        return float(self.backoff_seconds[index])


def escalation_reason(
    reasons: str | list[str] | tuple[str, ...] | None = None,
    *,
    gate: str | None = None,
    step: str | None = None,
    rounds_used: int | None = None,
    max_rounds: int = MAX_CORRECTION_ROUNDS,
) -> str:
    """Build a plain-language escalation message for the user.

    Accepts the failure ``reasons`` produced by a gate check (a single
    string or a list of strings) plus optional context about which gate or
    step failed and how many correction rounds were used.  Always returns
    a non-empty, human-readable sentence — never a stack trace.
    """
    parts: list[str] = []

    where = gate or step
    if where:
        label = where if where.lower().startswith("gate") else f"Step '{where}'"
        parts.append(f"{label} could not pass, so this video is paused.")
    else:
        parts.append("This video is paused and needs a decision.")

    if reasons:
        if isinstance(reasons, str):
            detail = [reasons]
        else:
            detail = [str(r) for r in reasons if str(r).strip()]
        if detail:
            parts.append(" ".join(detail))

    if rounds_used is not None and rounds_used >= max_rounds:
        parts.append(
            f"The automatic fix was already tried {rounds_used} times "
            f"(the limit is {max_rounds}), so it will not be retried again."
        )

    parts.append("Only this video is affected; other videos keep going.")
    return " ".join(parts)
