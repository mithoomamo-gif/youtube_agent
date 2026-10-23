"""Compliance Gate — pure visibility decision logic (Round 6).

This module is deliberately dependency-free so every publish path
(the publishing workflow / Gate C, the fake-provider publish stage,
the execution publish finaliser, and the real-shaped YouTube
resumable uploader) can share exactly one rule:

* While the overall compliance state is not ``passed``:
  - the effective visibility of any upload is **private**, whatever
    was requested (``forced_private=True``), and
  - a request to publish **publicly** is refused; the typed reason
    code is :data:`REASON_CODE` (``compliance_not_passed``).
* When the overall state is ``passed``, the requested visibility is
  honoured unchanged.

Nothing here touches the database — callers resolve the overall
state (see :mod:`app.compliance.service`) and hand it in as a bool,
an overall-state string, a state dict, or a callable returning any
of those.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Union

__all__ = [
    "REASON_CODE",
    "REASON_MESSAGE",
    "coerce_overall",
    "is_passed",
    "resolve_checker",
    "evaluate_visibility",
    "ComplianceDecision",
]

#: Typed reason code carried by every compliance refusal/disclosure.
REASON_CODE = "compliance_not_passed"

REASON_MESSAGE = (
    "Public publishing is blocked until the compliance audit passes "
    "— uploads are forced to Private."
)

OverallLike = Union[bool, str, dict, Callable[[], Any], None]


def coerce_overall(value: Any) -> Optional[str]:
    """Normalise a compliance value to an overall-state string.

    Accepts ``True``/``False``, an overall string (``passed`` /
    ``in_progress`` / ``not_started``), or a state dict carrying
    ``overall`` (or a boolean ``passed`` / ``public_publishing_allowed``).
    Returns ``None`` when the value carries no usable state.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "passed" if value else "not_started"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("passed", "pass", "approved", "true"):
            return "passed"
        if lowered in ("in_progress", "in-progress", "started"):
            return "in_progress"
        if lowered in ("not_started", "pending", "false", ""):
            return "not_started"
        return None
    if isinstance(value, dict):
        overall = value.get("overall")
        if isinstance(overall, str):
            return coerce_overall(overall)
        for key in ("passed", "public_publishing_allowed", "compliance_passed"):
            if isinstance(value.get(key), bool):
                return "passed" if value[key] else "not_started"
        items = value.get("items")
        if isinstance(items, list) and items:
            statuses = [
                str(item.get("status"))
                for item in items
                if isinstance(item, dict) and item.get("status")
            ]
            if statuses and all(s == "passed" for s in statuses):
                return "passed"
            return "in_progress" if any(s != "pending" for s in statuses) else "not_started"
    return None


def is_passed(value: Any) -> bool:
    """True only for an explicit overall ``passed``."""
    return coerce_overall(value) == "passed"


def resolve_checker(checker: OverallLike) -> Optional[str]:
    """Resolve a checker (value or zero-arg callable) to an overall.

    ``None`` in -> ``None`` out: NO gate was configured, which callers
    treat as the legacy ungated behaviour.  A configured checker that
    yields nothing usable resolves to ``not_started`` — a gate that
    cannot prove a pass must never act as one.
    """
    if checker is None:
        return None
    value = checker() if callable(checker) else checker
    return coerce_overall(value) or "not_started"


class ComplianceDecision(dict):
    """Visibility decision as a plain dict (JSON-ready disclosure).

    Keys: ``overall``, ``passed``, ``requested_visibility``,
    ``effective_visibility``, ``forced_private``,
    ``public_publish_refused``, ``reason_code``, ``message``.
    """

    @property
    def passed(self) -> bool:
        return bool(self.get("passed"))

    @property
    def forced_private(self) -> bool:
        return bool(self.get("forced_private"))

    @property
    def effective_visibility(self) -> str:
        return str(self.get("effective_visibility") or "private")


def evaluate_visibility(
    requested_visibility: Optional[str],
    overall: Any,
) -> ComplianceDecision:
    """The one visibility rule, applied to one publish request.

    ``requested_visibility`` defaults to ``public`` (a publish is, by
    intent, a request to go public).  Pre-PASS the effective
    visibility is always ``private``; a ``public`` request is
    additionally marked ``public_publish_refused`` so callers that
    attempt the public flip itself can refuse it with the typed
    reason instead of merely downgrading it.
    """
    requested = (requested_visibility or "public").strip().lower()
    if requested not in ("public", "unlisted", "private"):
        requested = "public"
    overall_state = coerce_overall(overall) or "not_started"
    passed = overall_state == "passed"
    if passed:
        return ComplianceDecision(
            overall=overall_state,
            passed=True,
            requested_visibility=requested,
            effective_visibility=requested,
            forced_private=False,
            public_publish_refused=False,
            reason_code=None,
            message="Compliance audit passed — requested visibility honoured.",
        )
    return ComplianceDecision(
        overall=overall_state,
        passed=False,
        requested_visibility=requested,
        effective_visibility="private",
        forced_private=requested != "private",
        public_publish_refused=requested == "public",
        reason_code=REASON_CODE,
        message=REASON_MESSAGE,
    )
