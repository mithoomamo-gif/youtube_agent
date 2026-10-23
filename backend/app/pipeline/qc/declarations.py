"""Planner pre-declarations -> QC annotation (annotation ONLY).

Round 4, Gate B conditional-pass workstream.  A planner may declare,
in advance, that a scene is intentionally ``"dark"`` and/or
``"still"`` (:class:`app.pipeline.scenes.models.DeclaredCondition`,
carried on ``SceneRecord.declared_conditions``).  When technical QC
later flags a matching passage in that scene's time window, the flag
record is *annotated* as pre-declared so a human reviewer (and the
Gate B conditional-pass policy) can see the flag was expected.

Hard rules — this module is deliberately powerless:

* Annotation NEVER suppresses or removes a flag / reason code.
* Annotation NEVER changes a check verdict or the report verdict.
* Annotation NEVER changes any threshold.  In particular it does NOT
  use (and is unrelated to) the pre-existing *whitelisting* inputs of
  :mod:`.technical` (``declared_still_ranges`` /
  ``declared_silence_ranges`` with their >= 80% overlap rule): those
  classify a detection as ``"declared"`` and keep it from escalating.
  A planner pre-declaration here has no such effect — a declared-dark
  brief black segment is still ``WARN`` with ``BLACK_SEGMENT_BRIEF``,
  byte-identical to the undeclared case; it merely also carries the
  annotation below.

Where the join happens, and why
-------------------------------
QC detections are time ranges, and scenes carry timings
(``SceneRecord.start_s`` / ``end_s`` on the measured narration spine,
which is also the rendered video's timeline), so the honest join is
**by time overlap at the per-detection (range) level**: each entry of
``CheckResult.details["ranges"]`` for the ``black_frames`` /
``freeze_frames`` checks is one flagged segment, and it is annotated
when it overlaps (any positive overlap) a declared scene carrying the
matching condition.  The check-level ``details`` additionally gains a
summary ``"declared"`` boolean (plus condition/note when true).

Annotation shape (per range entry)::

    {"declared": True,
     "declared_condition": "dark",          # or "still"
     "note": "pre-declared by planner",
     "declared_scene_id": "<scene_id>"}     # when the scene has one

Consistency rule for undeclared occurrences: EVERY range entry of the
two annotatable checks that passes through this layer gains a
``"declared"`` key — ``False`` (with no ``declared_condition`` /
``note`` keys) when no matching declaration overlaps it.  There is no
third state.

Flag mapping (planner condition -> the QC flag it pre-declares)::

    "dark"  -> BLACK_SEGMENT_BRIEF     (check_id "black_frames")
    "still" -> FREEZE_DECLARED_STILL   (check_id "freeze_frames")

Note on freeze codes: the classifier in :mod:`.technical` emits the
*warn* reason ``FREEZE_DETECTED`` for an undeclared freeze and the
*declared-whitelist* reason ``FREEZE_DECLARED_STILL`` only for the
separate ``declared_still_ranges`` input.  Annotation here applies to
the ``freeze_frames`` check as a whole, whichever of those codes it
carries — and changes neither.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..scenes.models import DeclaredCondition

__all__ = [
    "ANNOTATION_NOTE",
    "CONDITION_FOR_CHECK_ID",
    "FLAG_CODE_FOR_CONDITION",
    "SceneDeclaration",
    "declarations_from_scenes",
    "annotate_check_result",
    "annotate_qc_checks",
    "annotate_qc_report",
]

#: Note text stamped on every positively-annotated flag record.
ANNOTATION_NOTE = "pre-declared by planner"

#: check_id -> the planner condition that pre-declares its flags.
CONDITION_FOR_CHECK_ID: dict[str, DeclaredCondition] = {
    "black_frames": DeclaredCondition.DARK,
    "freeze_frames": DeclaredCondition.STILL,
}

#: planner condition -> the QC flag code it pre-declares (see docstring
#: for the freeze-code nuance).
FLAG_CODE_FOR_CONDITION: dict[DeclaredCondition, str] = {
    DeclaredCondition.DARK: "BLACK_SEGMENT_BRIEF",
    DeclaredCondition.STILL: "FREEZE_DECLARED_STILL",
}


# ---------------------------------------------------------------------------
# Declaration model (QC-side view of a declared scene)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SceneDeclaration:
    """One scene's declared visual conditions, with its time window.

    ``conditions`` holds :class:`DeclaredCondition` values normalised
    to their plain string forms (``"dark"`` / ``"still"``).
    """

    start_s: float
    end_s: float
    conditions: tuple = ()  # tuple[str, ...] of DeclaredCondition values
    scene_id: Optional[str] = None

    def declares(self, condition: DeclaredCondition) -> bool:
        return condition.value in self.conditions

    def overlaps(self, start_s: float, end_s: float) -> bool:
        """Any positive time overlap with ``[start_s, end_s)``."""
        return min(self.end_s, end_s) - max(self.start_s, start_s) > 1e-9


def _normalise_conditions(raw: Any) -> tuple:
    values: list[str] = []
    if raw is None:
        return ()
    if isinstance(raw, (str, DeclaredCondition)):
        raw = [raw]
    for item in raw or ():
        value = item.value if isinstance(item, DeclaredCondition) else str(item)
        if value in (DeclaredCondition.DARK.value, DeclaredCondition.STILL.value):
            if value not in values:
                values.append(value)
    return tuple(values)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def declarations_from_scenes(scenes: Optional[Iterable[Any]]) -> tuple:
    """Extract :class:`SceneDeclaration`\\ s from planner scenes.

    Duck-typed: each item may be a ``SceneRecord``, a manifest dict, a
    :class:`SceneDeclaration`, or any object carrying ``start_s`` /
    ``end_s`` / ``declared_conditions`` (``scene_id`` optional).
    Scenes with no declared conditions contribute nothing.
    """
    declarations: list[SceneDeclaration] = []
    for scene in scenes or ():
        if isinstance(scene, SceneDeclaration):
            if scene.conditions:
                declarations.append(scene)
            continue
        conditions = _normalise_conditions(_get(scene, "declared_conditions"))
        if not conditions:
            continue
        try:
            start_s = float(_get(scene, "start_s"))
            end_s = float(_get(scene, "end_s"))
        except (TypeError, ValueError):
            continue
        declarations.append(
            SceneDeclaration(
                start_s=start_s,
                end_s=end_s,
                conditions=conditions,
                scene_id=_get(scene, "scene_id"),
            )
        )
    return tuple(declarations)


# ---------------------------------------------------------------------------
# Annotation (pure: returns NEW check/report objects, inputs untouched)
# ---------------------------------------------------------------------------


def _matching_declaration(
    declarations: Sequence[SceneDeclaration],
    condition: DeclaredCondition,
    start_s: float,
    end_s: float,
) -> Optional[SceneDeclaration]:
    for declaration in declarations:
        if declaration.declares(condition) and declaration.overlaps(start_s, end_s):
            return declaration
    return None


def annotate_check_result(check: Any, declarations: Sequence[SceneDeclaration]) -> Any:
    """Return ``check`` with per-range declared annotations added.

    Only the ``black_frames`` / ``freeze_frames`` checks are
    annotatable; any other check is returned unchanged.  The returned
    check has the SAME ``check_id``, SAME ``verdict`` and SAME
    ``reason_codes`` as the input — only ``details`` gains keys.
    """
    condition = CONDITION_FOR_CHECK_ID.get(getattr(check, "check_id", ""))
    if condition is None:
        return check
    details = dict(getattr(check, "details", None) or {})
    ranges = details.get("ranges")
    if not isinstance(ranges, (list, tuple)):
        return check

    annotated_ranges: list[dict] = []
    any_declared = False
    for entry in ranges:
        new_entry = dict(entry)
        try:
            start_s = float(entry.get("start_s"))
            end_s = float(entry.get("end_s"))
        except (TypeError, ValueError, AttributeError):
            new_entry["declared"] = False
            annotated_ranges.append(new_entry)
            continue
        match = _matching_declaration(declarations, condition, start_s, end_s)
        if match is None:
            # Consistent undeclared state: declared=False, and no
            # declared_condition / note keys at all.
            new_entry["declared"] = False
        else:
            any_declared = True
            new_entry["declared"] = True
            new_entry["declared_condition"] = condition.value
            new_entry["note"] = ANNOTATION_NOTE
            if match.scene_id is not None:
                new_entry["declared_scene_id"] = match.scene_id
        annotated_ranges.append(new_entry)

    details["ranges"] = annotated_ranges
    details["declared"] = any_declared
    if any_declared:
        details["declared_condition"] = condition.value
        details["note"] = ANNOTATION_NOTE
    return dataclasses.replace(check, details=details)


def annotate_qc_checks(
    checks: Iterable[Any], declarations: Sequence[SceneDeclaration]
) -> tuple:
    """Annotate a sequence of check results (see :func:`annotate_check_result`)."""
    return tuple(annotate_check_result(c, declarations) for c in checks)


def annotate_qc_report(report: Any, declarations: Sequence[SceneDeclaration]) -> Any:
    """Return a copy of a QC report with its checks annotated.

    Verdicts, reason codes, detections, probe and loudness are carried
    over untouched — only check ``details`` gain annotation keys.
    """
    return dataclasses.replace(
        report, checks=annotate_qc_checks(report.checks, declarations)
    )
