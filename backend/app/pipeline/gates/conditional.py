"""Gate B conditional-pass policy for minor QC warnings.

Policy (plain statement)
------------------------
The roadmap default publishing mode is **review-first**.  A QC ``WARN``
whose flags are *all* in the explicit, versioned minor-flag class
:data:`MINOR_QC_FLAGS` is classified as **conditional** -- it is NOT a
pass.  A conditional result may only proceed in ``review_first`` mode
after a recorded human approval; without that approval it stays pending
and is not allowed.  In **full-auto** (``auto``) mode a conditional
result is **always blocked**, regardless of any approval -- full-auto
never accepts a conditional.  Any ``WARN`` carrying a non-minor flag,
a ``WARN`` with no flags at all, and any ``BLOCK`` / ``FAIL`` verdict
are hard blocks.  The policy fails closed: anything missing or
ambiguous is a block, never a pass.

This module is a *policy layer only*.  It does not change QC thresholds
or QC detection (see ``pipeline.qc.technical``); it only classifies an
already-issued QC verdict + flags and decides what Gate B may do with
that classification under a given publishing mode.

Subtitle script-match scores (R4-005)
--------------------------------------
Neither the strict ``script_match_ratio`` nor the transliteration-aware
fair ``script_match_ratio_fair`` (see
``app.pipeline.subtitles.validation``) is an input to this policy: no
documented WARN rule references subtitle scores, and classification
here reads the QC verdict + reason-code flags ONLY.  Both subtitle
scores are carried in production QC reports for reporting / WARN
context under the named policy
``SUBTITLE_SCRIPT_MATCH_GATE_POLICY = "report_only"``; wiring either
score into a Gate B threshold would be a separate, explicit policy
change, not a side effect of reporting it.

Minor-flag class
----------------
``MINOR_QC_FLAGS`` is versioned by :data:`MINOR_FLAG_POLICY_VERSION`.
Adding a flag to the class is a policy change and must bump the
version.  The starting class (v1.0) contains exactly two flags that
describe brief, real-footage artefacts rather than defects:

* ``BLACK_SEGMENT_BRIEF`` -- a brief black passage (e.g. a night scene
  in real stock footage) below the ``BLACK_SEGMENT_TOO_LONG`` block
  threshold.
* ``FREEZE_DECLARED_STILL`` -- a freeze frame that was declared as a
  still image by the edit plan, not an accidental render stall.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Union

from .tokens import Verdict

__all__ = [
    "MINOR_FLAG_POLICY_VERSION",
    "MINOR_QC_FLAGS",
    "QcClassification",
    "GateBDecision",
    "classify_qc",
    "evaluate_conditional",
]


# ---------------------------------------------------------------------------
# Minor-flag policy (versioned)
# ---------------------------------------------------------------------------

MINOR_FLAG_POLICY_VERSION = "1.0"

MINOR_QC_FLAGS: frozenset[str] = frozenset(
    {"BLACK_SEGMENT_BRIEF", "FREEZE_DECLARED_STILL"}
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QcClassification:
    """Classification of a QC verdict + flags under the minor-flag policy.

    ``outcome`` is one of ``"pass"`` | ``"conditional"`` | ``"block"``.
    ``minor_flags`` / ``non_minor_flags`` partition the (normalised)
    input flags.  ``policy_version`` records the minor-flag policy
    version the classification was made under.  ``reasons`` are
    human-readable explanations of the outcome.
    """

    outcome: str  # "pass" | "conditional" | "block"
    minor_flags: tuple = ()
    non_minor_flags: tuple = ()
    policy_version: str = MINOR_FLAG_POLICY_VERSION
    reasons: tuple = ()


@dataclass(frozen=True)
class GateBDecision:
    """Gate B decision for a :class:`QcClassification` under a mode.

    ``outcome`` is one of ``"pass"`` | ``"conditional_pending_approval"``
    | ``"conditional_approved"`` | ``"blocked_full_auto"`` | ``"block"``.
    ``allowed`` is True only for a clean pass and for a conditional
    that was approved in review-first mode.
    """

    outcome: str  # "pass" | "conditional_pending_approval" | "conditional_approved" | "blocked_full_auto" | "block"
    allowed: bool = False
    reasons: tuple = ()


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _normalize_verdict(
    qc_verdict: Union[Verdict, str, None],
) -> str:
    """Return ``"PASS"`` / ``"WARN"`` / ``"BLOCK"`` / raw upper string.

    Accepts the :class:`Verdict` enum and plain strings
    case-insensitively.  ``"FAIL"`` is normalised to ``"BLOCK"``.
    Unknown / missing values are returned upper-cased (or ``""``) so
    the caller can fail closed.
    """
    if qc_verdict is None:
        return ""
    if isinstance(qc_verdict, Verdict):
        return qc_verdict.value.upper()
    text = str(qc_verdict).strip().upper()
    # Verdict enum values are already upper; also accept enum-like
    # strings such as "Verdict.WARN".
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    if text == "FAIL":
        return "BLOCK"
    return text


def _normalize_flags(qc_flags: Union[Iterable[str], str, None]) -> tuple:
    """Normalise flags to a de-duplicated tuple of upper-case codes.

    Preserves first-seen order.  A single string is treated as one
    flag (not iterated character-by-character).  ``None`` -> ``()``.
    """
    if qc_flags is None:
        return ()
    if isinstance(qc_flags, str):
        items: Iterable = (qc_flags,)
    else:
        items = qc_flags
    seen: list = []
    seen_set: set = set()
    for item in items:
        if item is None:
            continue
        code = str(item).strip().upper()
        if not code:
            continue
        # Accept enum-like values via their .value when present.
        if code not in seen_set:
            seen_set.add(code)
            seen.append(code)
    return tuple(seen)


def _normalize_mode(mode: Union[str, None]) -> str:
    """Normalise a publishing mode string.

    ``"review_first"`` (also ``"review-first"`` / ``"reviewfirst"``)
    stays review-first; ``"auto"`` / ``"full_auto"`` /
    ``"full-auto"`` normalise to ``"auto"``.  Anything else is
    returned lower-cased so the caller can fail closed.
    """
    if mode is None:
        return ""
    text = str(mode).strip().lower().replace("-", "_").replace(" ", "_")
    if text in ("full_auto", "fullauto"):
        return "auto"
    if text == "reviewfirst":
        return "review_first"
    return text


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_qc(
    qc_verdict: Union[Verdict, str],
    qc_flags: Union[Iterable[str], str, None] = (),
) -> QcClassification:
    """Classify a QC verdict + flags under the minor-flag policy.

    Semantics (fail closed):

    * verdict ``PASS`` -> ``"pass"``
    * verdict ``WARN`` and flags non-empty and ALL in
      :data:`MINOR_QC_FLAGS` -> ``"conditional"``
    * verdict ``WARN`` with ANY non-minor flag OR empty flags ->
      ``"block"`` (a WARN with no flags is not classifiable as minor)
    * verdict ``BLOCK`` / ``FAIL`` -> ``"block"``
    * any unknown / missing verdict -> ``"block"``
    """
    verdict = _normalize_verdict(qc_verdict)
    flags = _normalize_flags(qc_flags)
    minor_flags = tuple(f for f in flags if f in MINOR_QC_FLAGS)
    non_minor_flags = tuple(f for f in flags if f not in MINOR_QC_FLAGS)

    if verdict == "PASS":
        return QcClassification(
            outcome="pass",
            minor_flags=minor_flags,
            non_minor_flags=non_minor_flags,
            policy_version=MINOR_FLAG_POLICY_VERSION,
            reasons=(
                "QC verdict is PASS; no conditional handling required.",
            ),
        )

    if verdict == "WARN":
        if not flags:
            return QcClassification(
                outcome="block",
                minor_flags=(),
                non_minor_flags=(),
                policy_version=MINOR_FLAG_POLICY_VERSION,
                reasons=(
                    "QC verdict is WARN with no flags; a flagless WARN "
                    "is not classifiable as minor and fails closed to block.",
                ),
            )
        if non_minor_flags:
            return QcClassification(
                outcome="block",
                minor_flags=minor_flags,
                non_minor_flags=non_minor_flags,
                policy_version=MINOR_FLAG_POLICY_VERSION,
                reasons=(
                    "QC verdict is WARN with non-minor flag(s) "
                    f"{list(non_minor_flags)}; only minor flags "
                    f"{sorted(MINOR_QC_FLAGS)} can be conditional.",
                ),
            )
        # WARN, non-empty, all minor.
        return QcClassification(
            outcome="conditional",
            minor_flags=minor_flags,
            non_minor_flags=(),
            policy_version=MINOR_FLAG_POLICY_VERSION,
            reasons=(
                "QC verdict is WARN with only minor flag(s) "
                f"{list(minor_flags)} under minor-flag policy "
                f"v{MINOR_FLAG_POLICY_VERSION}; classified as conditional "
                "(not a pass).",
            ),
        )

    if verdict == "BLOCK":
        return QcClassification(
            outcome="block",
            minor_flags=minor_flags,
            non_minor_flags=non_minor_flags,
            policy_version=MINOR_FLAG_POLICY_VERSION,
            reasons=(
                "QC verdict is BLOCK; hard block, conditional policy "
                "does not apply.",
            ),
        )

    # Unknown / missing verdict -- fail closed.
    return QcClassification(
        outcome="block",
        minor_flags=minor_flags,
        non_minor_flags=non_minor_flags,
        policy_version=MINOR_FLAG_POLICY_VERSION,
        reasons=(
            f"Unrecognised QC verdict {qc_verdict!r}; failing closed to block.",
        ),
    )


# ---------------------------------------------------------------------------
# Gate B decision
# ---------------------------------------------------------------------------


def evaluate_conditional(
    classification: QcClassification,
    mode: Union[str, None],
    has_recorded_approval: bool = False,
) -> GateBDecision:
    """Decide Gate B handling for ``classification`` under ``mode``.

    Semantics:

    * classification ``"pass"`` -> outcome ``"pass"``, allowed True.
    * ``"conditional"`` + mode ``"review_first"`` + no approval ->
      ``"conditional_pending_approval"``, allowed False.
    * ``"conditional"`` + mode ``"review_first"`` + approval ->
      ``"conditional_approved"``, allowed True.
    * ``"conditional"`` + mode ``"auto"`` ->
      ``"blocked_full_auto"``, allowed False REGARDLESS of approval
      (full-auto never accepts conditional).
    * classification ``"block"`` -> ``"block"``, allowed False.
    * unknown mode / unknown classification outcome -> fail closed
      to ``"block"`` (or ``"blocked_full_auto"`` is never inferred
      for an unknown mode).
    """
    outcome = ""
    if classification is not None:
        outcome = str(getattr(classification, "outcome", "") or "").strip().lower()

    normalized_mode = _normalize_mode(mode)
    approved = bool(has_recorded_approval)

    if outcome == "pass":
        return GateBDecision(
            outcome="pass",
            allowed=True,
            reasons=(
                "QC classification is pass; Gate B QC check passes "
                "without conditional handling.",
            ),
        )

    if outcome == "block":
        return GateBDecision(
            outcome="block",
            allowed=False,
            reasons=(
                "QC classification is block; Gate B is blocked and "
                "no approval or mode can override it.",
            ),
        )

    if outcome == "conditional":
        if normalized_mode == "auto":
            return GateBDecision(
                outcome="blocked_full_auto",
                allowed=False,
                reasons=(
                    "Conditional QC result is blocked in full-auto mode; "
                    "full-auto never accepts a conditional, regardless "
                    "of any recorded approval. Conditional is not a pass.",
                ),
            )
        if normalized_mode == "review_first":
            if approved:
                return GateBDecision(
                    outcome="conditional_approved",
                    allowed=True,
                    reasons=(
                        "Conditional QC result approved by recorded "
                        "human approval in review-first mode; allowed "
                        "to proceed. Conditional is not a pass -- it "
                        "proceeds only via this approval.",
                    ),
                )
            return GateBDecision(
                outcome="conditional_pending_approval",
                allowed=False,
                reasons=(
                    "Conditional QC result in review-first mode is "
                    "pending recorded human approval; not allowed "
                    "until approval is recorded. Conditional is not "
                    "a pass.",
                ),
            )
        # Unknown mode -- fail closed.
        return GateBDecision(
            outcome="block",
            allowed=False,
            reasons=(
                f"Conditional QC result with unrecognised mode {mode!r}; "
                "failing closed to block.",
            ),
        )

    # Unknown classification outcome -- fail closed.
    return GateBDecision(
        outcome="block",
        allowed=False,
        reasons=(
            f"Unrecognised QC classification outcome {outcome!r}; "
            "failing closed to block.",
        ),
    )
