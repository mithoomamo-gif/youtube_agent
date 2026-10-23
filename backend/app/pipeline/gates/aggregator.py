"""Gate B (pre-publish) aggregator.

Gate B is satisfied only when PASS tokens from all three inspecting
agents -- Technical QC (Agent 32), Copyright (Agent 33) and Policy
(Agent 34) -- are presented together, and every token is bound to the
*same* publish-manifest hash as the video about to be uploaded
(MASTER_ROADMAP Part 7.5).  A WARN token never satisfies Gate B, a token
issued against an older manifest never satisfies Gate B, and a tampered
token never satisfies Gate B.  The check fails closed: anything missing
or ambiguous is reported as a problem, never waved through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .tokens import (
    GATE_COPYRIGHT,
    GATE_POLICY_SIMILARITY,
    GATE_QC_TECHNICAL,
    GateToken,
    Verdict,
    token_binding_problems,
)

__all__ = [
    "REQUIRED_GATE_B_TOKENS",
    "GateBProblem",
    "GateBResult",
    "GateBPolicyResult",
    "verify_gate_b",
    "evaluate_gate_b",
    "gate_b_verdict_label",
    "collect_gate_b_reason_codes",
]

# ---------------------------------------------------------------------------
# Conditional-pass contract (Worker 1: pipeline.gates.conditional)
# ---------------------------------------------------------------------------
# The constants / helpers below are a *fallback* used only when
# ``conditional`` has not landed yet (parallel Round-4 teams).  When the
# real module is present it is always preferred.  The fallback encodes
# exactly the contract in the task brief and nothing more; QC verdicts
# and thresholds themselves are never changed here.
_FALLBACK_MINOR_QC_FLAGS = frozenset({"BLACK_SEGMENT_BRIEF", "FREEZE_DECLARED_STILL"})
_FALLBACK_POLICY_VERSION = "1.0"


@dataclass(frozen=True)
class _FallbackClassification:
    outcome: str
    minor_flags: tuple = ()
    non_minor_flags: tuple = ()
    policy_version: str = _FALLBACK_POLICY_VERSION
    reasons: tuple = ()


@dataclass(frozen=True)
class _FallbackDecision:
    outcome: str
    allowed: bool = False
    reasons: tuple = ()


def _fallback_classify_qc(qc_verdict: Any, qc_flags: Any = ()) -> _FallbackClassification:
    verdict_value = str(getattr(qc_verdict, "value", qc_verdict) or "").upper()
    flags = tuple(str(f) for f in (qc_flags or ()))
    minor = tuple(f for f in flags if f in _FALLBACK_MINOR_QC_FLAGS)
    non_minor = tuple(f for f in flags if f not in _FALLBACK_MINOR_QC_FLAGS)
    if verdict_value == Verdict.PASS.value:
        return _FallbackClassification(
            outcome="pass", minor_flags=(), non_minor_flags=(),
            reasons=("QC PASS",),
        )
    if verdict_value == Verdict.WARN.value and flags and not non_minor:
        return _FallbackClassification(
            outcome="conditional", minor_flags=minor, non_minor_flags=(),
            reasons=("QC WARN with minor-only flags",),
        )
    return _FallbackClassification(
        outcome="block", minor_flags=minor, non_minor_flags=non_minor,
        reasons=(f"QC {verdict_value or 'UNKNOWN'} is not conditionally passable",),
    )


def _fallback_evaluate_conditional(classification: Any, mode: str, has_recorded_approval: bool) -> _FallbackDecision:
    outcome = str(getattr(classification, "outcome", "block"))
    if outcome == "pass":
        return _FallbackDecision(outcome="pass", allowed=True, reasons=("QC PASS",))
    if outcome == "conditional":
        if str(mode) == "auto":
            return _FallbackDecision(
                outcome="block", allowed=False,
                reasons=("conditional QC cannot publish in auto mode",),
            )
        if has_recorded_approval:
            return _FallbackDecision(
                outcome="conditional", allowed=True,
                reasons=("conditional QC released by recorded approval in review_first mode",),
            )
        return _FallbackDecision(
            outcome="conditional", allowed=False,
            reasons=("conditional QC requires a recorded approval in review_first mode",),
        )
    return _FallbackDecision(outcome="block", allowed=False, reasons=("QC blocked",))


def _load_conditional():
    """Return (classify_qc, evaluate_conditional) — real module if present."""
    try:
        from . import conditional as _cond  # type: ignore

        classify = getattr(_cond, "classify_qc", None)
        evaluate = getattr(_cond, "evaluate_conditional", None)
        if callable(classify) and callable(evaluate):
            return classify, evaluate
    except Exception:
        pass
    return _fallback_classify_qc, _fallback_evaluate_conditional


def _call_classify(classify_fn, verdict: Any, flags: tuple):
    """Call classify_qc tolerating Verdict-enum vs str verdict conventions."""
    try:
        return classify_fn(verdict, flags)
    except Exception:
        pass
    try:
        return classify_fn(str(getattr(verdict, "value", verdict)), flags)
    except Exception:
        pass
    try:
        return classify_fn(verdict, list(flags))
    except Exception:
        return _fallback_classify_qc(verdict, flags)


def _call_evaluate(evaluate_fn, classification: Any, mode: str, has_recorded_approval: bool):
    try:
        return evaluate_fn(classification, mode, has_recorded_approval)
    except Exception:
        pass
    try:
        return evaluate_fn(
            classification, mode=mode, has_recorded_approval=has_recorded_approval
        )
    except Exception:
        return _fallback_evaluate_conditional(classification, mode, has_recorded_approval)


def _as_tuple(value: Any) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return (value,)


def _record_audit_event(sink_path: Any, event: dict) -> None:
    """Best-effort audit via Worker 3's module; no-op if not landed yet."""
    if sink_path is None:
        return
    try:
        from .audit import record_gate_b_event  # type: ignore

        record_gate_b_event(sink_path, event)
    except Exception:
        return

# Gate B group name -> the gate id whose PASS token satisfies the group.
# The copyright group is satisfied by the aggregate copyright token
# (G1+G2+G6 combined, see copyright.CopyrightReport.to_token).
REQUIRED_GATE_B_TOKENS: Mapping[str, str] = {
    "qc": GATE_QC_TECHNICAL,
    "copyright": GATE_COPYRIGHT,
    "policy": GATE_POLICY_SIMILARITY,
}


@dataclass(frozen=True)
class GateBProblem:
    group: str  # "qc" | "copyright" | "policy"
    expected_gate_id: str
    code: str  # MISSING_TOKEN | MANIFEST_HASH_MISMATCH | TOKEN_TAMPERED | VERDICT_NOT_PASS
    detail: str


@dataclass(frozen=True)
class GateBResult:
    allowed: bool
    manifest_hash: str
    satisfied_groups: tuple  # group names that presented a valid PASS token
    problems: tuple  # tuple[GateBProblem, ...]

    @property
    def verdict(self) -> Verdict:
        return Verdict.PASS if self.allowed else Verdict.BLOCK


def verify_gate_b(tokens: Iterable[GateToken], manifest_hash: str) -> GateBResult:
    """Check a set of gate tokens against Gate B for ``manifest_hash``."""
    by_gate_id: dict = {}
    for token in tokens:
        by_gate_id.setdefault(token.gate_id, token)

    satisfied: list = []
    problems: list = []
    for group, gate_id in REQUIRED_GATE_B_TOKENS.items():
        token = by_gate_id.get(gate_id)
        if token is None:
            problems.append(
                GateBProblem(
                    group=group,
                    expected_gate_id=gate_id,
                    code="MISSING_TOKEN",
                    detail=f"no token presented for gate '{gate_id}'",
                )
            )
            continue
        binding = token_binding_problems(token, manifest_hash)
        if binding:
            code = (
                "MANIFEST_HASH_MISMATCH"
                if "MANIFEST_HASH_MISMATCH" in binding
                else "TOKEN_TAMPERED"
            )
            problems.append(
                GateBProblem(
                    group=group,
                    expected_gate_id=gate_id,
                    code=code,
                    detail=f"token {token.token_id} failed binding checks: {binding}",
                )
            )
            continue
        if token.verdict is not Verdict.PASS:
            problems.append(
                GateBProblem(
                    group=group,
                    expected_gate_id=gate_id,
                    code="VERDICT_NOT_PASS",
                    detail=(
                        f"gate '{gate_id}' verdict is {token.verdict.value} "
                        f"(reasons: {list(token.reason_codes)}); Gate B requires PASS"
                    ),
                )
            )
            continue
        satisfied.append(group)

    return GateBResult(
        allowed=not problems,
        manifest_hash=manifest_hash,
        satisfied_groups=tuple(satisfied),
        problems=tuple(problems),
    )


# ---------------------------------------------------------------------------
# Conditional-pass aware evaluation (ADDITIVE — verify_gate_b unchanged)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateBPolicyResult:
    """Extended Gate B result that understands QC conditional passes.

    ``allowed`` is the publish decision under the conditional policy.
    ``outcome`` is ``"pass"`` (all PASS), ``"conditional"`` (QC WARN
    with minor-only flags — release depends on mode + approval) or
    ``"block"``.  ``classification`` / ``decision`` are the objects
    returned by ``conditional.classify_qc`` /
    ``conditional.evaluate_conditional`` (or the local fallback with the
    same field shape).  ``base_result`` is the untouched
    :class:`GateBResult` from :func:`verify_gate_b`, so callers can
    always see the strict PASS-only verdict.
    """

    allowed: bool
    outcome: str  # "pass" | "conditional" | "block"
    classification: Any = None
    decision: Any = None
    problems: tuple = ()
    manifest_hash: str = ""
    satisfied_groups: tuple = ()
    base_result: Optional[GateBResult] = None

    @property
    def verdict(self) -> Verdict:
        return Verdict.PASS if self.allowed else Verdict.BLOCK

    # Convenience views over the classification (None-safe).
    @property
    def minor_flags(self) -> tuple:
        return _as_tuple(getattr(self.classification, "minor_flags", ())) if self.classification else ()

    @property
    def non_minor_flags(self) -> tuple:
        return _as_tuple(getattr(self.classification, "non_minor_flags", ())) if self.classification else ()

    @property
    def policy_version(self) -> Optional[str]:
        return getattr(self.classification, "policy_version", None) if self.classification else None


def evaluate_gate_b(
    tokens: Iterable[GateToken],
    manifest_hash: str,
    *,
    mode: str = "review_first",
    has_recorded_approval: bool = False,
    audit_sink: Any = None,
    actor: str = "system",
    asset_ids: Optional[Iterable[str]] = None,
) -> GateBPolicyResult:
    """Evaluate Gate B including the QC conditional-pass policy.

    Copyright / policy groups are judged *exactly* as
    :func:`verify_gate_b` judges them.  Only the QC group differs:

    * QC ``PASS`` — existing behaviour.
    * QC ``WARN`` with minor-only flags (per
      ``conditional.classify_qc``) — *conditional*: allowed only when
      ``mode == "review_first"`` AND ``has_recorded_approval`` is true.
      In ``"auto"`` mode a conditional blocks even with approval.
    * QC ``WARN`` with any non-minor flag, or QC ``BLOCK`` — blocked,
      exactly as today.

    ``mode`` is a :data:`app.orchestration.state.PublishMode` value
    (``"review_first"`` | ``"auto"``).  When ``audit_sink`` is given,
    every conditional decision is recorded through Worker 3's
    ``audit.record_gate_b_event`` (guarded import; no-op if that
    module has not landed yet).
    """
    token_list = list(tokens)
    base = verify_gate_b(token_list, manifest_hash)

    qc_token = next(
        (t for t in token_list if t.gate_id == GATE_QC_TECHNICAL), None
    )
    if qc_token is None:
        return GateBPolicyResult(
            allowed=False, outcome="block", classification=None, decision=None,
            problems=base.problems, manifest_hash=manifest_hash,
            satisfied_groups=base.satisfied_groups, base_result=base,
        )
    if token_binding_problems(qc_token, manifest_hash):
        return GateBPolicyResult(
            allowed=False, outcome="block", classification=None, decision=None,
            problems=base.problems, manifest_hash=manifest_hash,
            satisfied_groups=base.satisfied_groups, base_result=base,
        )

    classify_fn, evaluate_fn = _load_conditional()
    qc_flags = tuple(str(c) for c in (qc_token.reason_codes or ()))
    classification = _call_classify(classify_fn, qc_token.verdict, qc_flags)
    cls_outcome = str(getattr(classification, "outcome", "block") or "block").lower()
    decision = _call_evaluate(evaluate_fn, classification, mode, bool(has_recorded_approval))

    if cls_outcome == "pass":
        return GateBPolicyResult(
            allowed=base.allowed,
            outcome="pass" if base.allowed else "block",
            classification=classification, decision=decision,
            problems=base.problems, manifest_hash=manifest_hash,
            satisfied_groups=base.satisfied_groups, base_result=base,
        )

    if cls_outcome == "conditional":
        other_problems = tuple(p for p in base.problems if p.group != "qc")
        decision_allowed = bool(getattr(decision, "allowed", False))
        if decision_allowed and not other_problems:
            # Preserve REQUIRED_GATE_B_TOKENS order for satisfied groups.
            satisfied = tuple(
                g for g in REQUIRED_GATE_B_TOKENS
                if g in base.satisfied_groups or g == "qc"
            )
            result = GateBPolicyResult(
                allowed=True, outcome="conditional",
                classification=classification, decision=decision,
                problems=(), manifest_hash=manifest_hash,
                satisfied_groups=satisfied, base_result=base,
            )
        else:
            if not decision_allowed:
                if str(mode) == "auto":
                    code = "CONDITIONAL_BLOCKED_AUTO_MODE"
                    detail = (
                        "QC WARN minor-only is conditional, but conditional "
                        "passes never publish in auto mode"
                    )
                elif not has_recorded_approval:
                    code = "CONDITIONAL_APPROVAL_REQUIRED"
                    detail = (
                        "QC WARN minor-only is conditional; a recorded "
                        "human approval in review_first mode is required"
                    )
                else:
                    code = "CONDITIONAL_BLOCKED"
                    detail = "QC conditional pass was not released"
                qc_problem = GateBProblem(
                    group="qc", expected_gate_id=GATE_QC_TECHNICAL,
                    code=code, detail=detail,
                )
                problems = other_problems + (qc_problem,)
            else:
                problems = other_problems
            result = GateBPolicyResult(
                allowed=False, outcome="conditional",
                classification=classification, decision=decision,
                problems=problems, manifest_hash=manifest_hash,
                satisfied_groups=base.satisfied_groups, base_result=base,
            )
        if audit_sink is not None:
            if result.allowed:
                event_type = "publish_released"
            elif str(mode) == "auto":
                event_type = "blocked"
            elif has_recorded_approval:
                event_type = "blocked"
            else:
                event_type = "conditional_token_minted"
            _record_audit_event(audit_sink, {
                "event_type": event_type,
                "manifest_hash": manifest_hash,
                "mode": str(mode),
                "outcome": result.outcome,
                "qc_flags": list(qc_flags),
                "minor_flags": list(result.minor_flags),
                "asset_ids": list(asset_ids or ()),
                "policy_version": result.policy_version,
                "actor": actor,
            })
        return result

    # block: WARN non-minor, BLOCK, or anything unclassifiable — as today.
    return GateBPolicyResult(
        allowed=False, outcome="block",
        classification=classification, decision=decision,
        problems=base.problems, manifest_hash=manifest_hash,
        satisfied_groups=base.satisfied_groups, base_result=base,
    )


# ---------------------------------------------------------------------------
# Shared verdict labelling (Round 6 closure, W7)
# ---------------------------------------------------------------------------
# ONE labelling rule for every Gate B surface: the package stage's
# finalize result / frozen manifest / package report AND the publish
# finaliser must describe the same evaluation with the same words.
# Before this helper existed, the package stage surfaced only the
# conditional-aware ``gate_b_passed`` bool (a conditional release read
# as a bare PASS, and a provenance block read as a bare FAIL), while
# the finaliser re-evaluated and reported only the strict problem
# code (``VERDICT_NOT_PASS``) -- two verdicts for one gate, diverging
# silently.  Callers now derive ``gate_b_verdict`` +
# ``gate_b_reason_codes`` from the SAME GateBPolicyResult via these
# helpers, so a label produced at package time is the label the
# finaliser will act on (given the same tokens / mode / approval).


def gate_b_verdict_label(outcome: Any, allowed: bool) -> str:
    """Canonical Gate B verdict label for one evaluation.

    * ``"pass"``             -- strict PASS (all groups PASS, Gate A ok)
    * ``"conditional_pass"`` -- QC WARN minor-only, RELEASED under the
      Round-4 rule (review_first + recorded approval).  Never a bare
      pass: the label itself carries the conditionality.
    * ``"conditional_blocked"`` -- QC WARN minor-only, NOT released
      (approval pending in review_first, or any conditional in auto
      mode).  The reason codes say which.
    * ``"block"``            -- hard block (any non-QC group failing,
      QC BLOCK / non-minor WARN, missing/tampered tokens, Gate A).
    """
    text = str(outcome or "block").strip().lower()
    if allowed and text == "conditional":
        return "conditional_pass"
    if allowed:
        return "pass"
    if text == "conditional":
        return "conditional_blocked"
    return "block"


def collect_gate_b_reason_codes(
    token_reason_codes: Iterable[Any] = (),
    problems: Iterable[Any] = (),
    extra: Iterable[Any] = (),
) -> tuple:
    """Ordered, de-duplicated Gate B reason codes for one evaluation.

    Token reason codes come first (they carry the typed gate reasons
    the strict verdict loses -- e.g. ``G2_PROVENANCE_FAILED`` from the
    copyright token, the QC flags behind a conditional), then the
    problem codes (``VERDICT_NOT_PASS``, ``MISSING_TOKEN``,
    ``CONDITIONAL_APPROVAL_REQUIRED``, ...), then any caller extras
    (e.g. ``GATE_A_NOT_PASSED``).  Both the package report and the
    finaliser's refusal detail are built from this same list, so the
    typed reason a reader sees at package time is the typed reason
    the finaliser reports when it refuses.
    """
    seen: list = []
    seen_set: set = set()

    def _add(code: Any) -> None:
        text = str(code or "").strip()
        if text and text not in seen_set:
            seen_set.add(text)
            seen.append(text)

    for code in token_reason_codes or ():
        _add(code)
    for problem in problems or ():
        _add(getattr(problem, "code", None) if not isinstance(problem, Mapping)
             else problem.get("code"))
    for code in extra or ():
        _add(code)
    return tuple(seen)
