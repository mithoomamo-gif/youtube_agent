"""Deterministic Retention Lint service (Agent 8 support).

This module contains NO LLM calls. Agent 8 (Hook & Retention) produces
a script plus a machine-readable sidecar; this lint validates that
sidecar against the cadence rules from roadmap section 33, so a script
cannot pass the retention pass on vibes alone.

Rules implemented (section 33, "Retention system"):

1. Verbal re-hook / open loop at least every 60-90 seconds of
   narration (~130-220 words depending on WPM). Chapter boundaries and
   production markers are used as proxies for re-hook positions.
2. No segment may run > 90 seconds without an interrupt marker.
   Rules 1 and 2 share the same gap computation: a gap longer than
   90 seconds' worth of words (``1.5 * target_wpm``) is a FAIL.
3. Production markers roughly every ~45 seconds, with deliberately
   VARIED spacing (e.g. 40/55/45/50s). Three or more identical
   consecutive gaps = WARN "predictable cadence".
4. Biggest payoff / climax at ~65-75% of runtime (word position).
5. Every chapter ends on an unresolved beat EXCEPT the final chapter.
6. No two consecutive chapters may use the same interrupt type; here
   checked in its narrow form: two consecutive chapters whose sole
   interrupt kind is identical -> WARN.
7. Hook anatomy 0-5s: PROOF. No greeting / channel intro EVER ->
   ``hook_has_greeting`` is a FAIL.
8. Hook must contain proof in the first 30 seconds ->
   ``hook_first_30s_has_proof=False`` is a FAIL.

Lint failures are FAILs Gate A cares about only via
:func:`retention_lint_passed` (True iff no ``fail`` issues); WARNs are
advisory.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "ChapterSpan",
    "InterruptMarker",
    "LintIssue",
    "ScriptSidecar",
    "lint_retention",
    "retention_lint_passed",
]

# Allowed production interrupt kinds (consumed by editing agents).
INTERRUPT_KINDS = (
    "visual-change",
    "zoom",
    "music-shift",
    "silence-beat",
    "on-screen-text",
)


class InterruptMarker(BaseModel):
    """A production interrupt marker at a word position in the script."""

    position_words: int
    kind: str  # visual-change | zoom | music-shift | silence-beat | on-screen-text


class ChapterSpan(BaseModel):
    """A chapter's word span and retention metadata."""

    chapter_id: str
    start_word: int
    end_word: int
    ends_on_unresolved: bool
    interrupt_kinds: list[str] = Field(default_factory=list)


class ScriptSidecar(BaseModel):
    """Machine-readable sidecar emitted alongside the narration text."""

    total_words: int
    target_wpm: int
    chapters: list[ChapterSpan] = Field(default_factory=list)
    markers: list[InterruptMarker] = Field(default_factory=list)
    climax_word: int | None = None
    hook_first_30s_has_proof: bool = False
    hook_has_greeting: bool = False


class LintIssue(BaseModel):
    """One retention-lint finding."""

    rule: str
    severity: Literal["fail", "warn"]
    message: str
    location: str


def _sorted_positions(sidecar: ScriptSidecar) -> list[int]:
    """All re-hook proxy positions: start, markers, chapter ends, end.

    Chapter boundaries are verbal re-hook proxies (chapters end on a
    mini-cliffhanger / open loop); markers are production interrupts.
    Both count for the gap rules.
    """
    positions: set[int] = {0, sidecar.total_words}
    for marker in sidecar.markers:
        if 0 <= marker.position_words <= sidecar.total_words:
            positions.add(marker.position_words)
    for chapter in sidecar.chapters:
        for pos in (chapter.start_word, chapter.end_word):
            if 0 <= pos <= sidecar.total_words:
                positions.add(pos)
    return sorted(positions)


def _marker_gaps(sidecar: ScriptSidecar) -> list[int]:
    """Gaps (in words) between consecutive marker positions.

    Includes the leading gap (0 -> first marker) and trailing gap
    (last marker -> total_words), since an unmarked head or tail is
    still a segment without an interrupt.
    """
    marker_positions = sorted(
        m.position_words for m in sidecar.markers if 0 <= m.position_words <= sidecar.total_words
    )
    points = [0, *marker_positions, sidecar.total_words]
    # De-duplicate consecutive identical points (e.g. marker at 0 or at end).
    deduped: list[int] = []
    for point in points:
        if not deduped or point != deduped[-1]:
            deduped.append(point)
    return [b - a for a, b in zip(deduped, deduped[1:]) if b - a > 0]


def lint_retention(sidecar: ScriptSidecar) -> list[LintIssue]:
    """Run all retention rules against ``sidecar``; return issues found."""
    issues: list[LintIssue] = []
    wpm = sidecar.target_wpm if sidecar.target_wpm > 0 else 130
    max_gap_words = 1.5 * wpm  # 90 seconds' worth of words

    # -- Rules 1 + 2: re-hook density / no segment > 90s without a marker.
    positions = _sorted_positions(sidecar)
    for start, end in zip(positions, positions[1:]):
        gap = end - start
        if gap > max_gap_words:
            issues.append(
                LintIssue(
                    rule="rehook_gap",
                    severity="fail",
                    message=(
                        f"Segment of {gap} words (~{gap / wpm * 60:.0f}s at {wpm} WPM) "
                        f"runs without a re-hook or interrupt marker; maximum is 90s "
                        f"({max_gap_words:.0f} words). Verbal re-hook required every "
                        f"60-90s and no segment may exceed 90s without a marker."
                    ),
                    location=f"words {start}-{end}",
                )
            )
    # A script with no markers/chapters at all is one giant segment;
    # the loop above covers it via positions == [0, total_words].

    # -- Rule 3: marker spacing must vary (predictable cadence warning).
    gaps = _marker_gaps(sidecar)
    run_value: int | None = None
    run_length = 0
    for idx, gap in enumerate(gaps):
        if gap == run_value:
            run_length += 1
        else:
            run_value = gap
            run_length = 1
        if run_length == 3:  # warn once per run of >= 3 identical gaps
            issues.append(
                LintIssue(
                    rule="predictable_cadence",
                    severity="warn",
                    message=(
                        f"Predictable cadence: {run_length} consecutive marker gaps of "
                        f"exactly {gap} words; vary marker spacing (e.g. 40/55/45/50s) "
                        f"so the cadence itself never becomes predictable."
                    ),
                    location=f"marker gap ending at gap index {idx}",
                )
            )

    # -- Rule 4: climax at 65-75% of total words.
    if sidecar.climax_word is not None and sidecar.total_words > 0:
        ratio = sidecar.climax_word / sidecar.total_words
        if ratio < 0.65 or ratio > 0.75:
            issues.append(
                LintIssue(
                    rule="climax_position",
                    severity="fail",
                    message=(
                        f"Climax at word {sidecar.climax_word} is {ratio:.0%} of the script; "
                        f"the biggest payoff must sit at 65-75% of runtime."
                    ),
                    location=f"word {sidecar.climax_word}",
                )
            )

    # -- Rule 5: every chapter except the last ends on an unresolved beat.
    for chapter in sidecar.chapters[:-1]:
        if not chapter.ends_on_unresolved:
            issues.append(
                LintIssue(
                    rule="chapter_unresolved_ending",
                    severity="fail",
                    message=(
                        f"Chapter {chapter.chapter_id} ends on a resolved beat; every "
                        f"chapter except the final one must end on an unresolved beat "
                        f"(mini-cliffhanger / open loop)."
                    ),
                    location=chapter.chapter_id,
                )
            )

    # -- Rule 6: no two consecutive chapters with the same single interrupt kind.
    for prev, curr in zip(sidecar.chapters, sidecar.chapters[1:]):
        if (
            len(prev.interrupt_kinds) == 1
            and len(curr.interrupt_kinds) == 1
            and prev.interrupt_kinds[0] == curr.interrupt_kinds[0]
        ):
            issues.append(
                LintIssue(
                    rule="repeated_interrupt_kind",
                    severity="warn",
                    message=(
                        f"Chapters {prev.chapter_id} and {curr.chapter_id} both use only "
                        f"the interrupt kind {curr.interrupt_kinds[0]!r}; vary interrupt "
                        f"types between consecutive chapters."
                    ),
                    location=f"{prev.chapter_id}->{curr.chapter_id}",
                )
            )

    # -- Rule 7: no greeting / intro, ever.
    if sidecar.hook_has_greeting:
        issues.append(
            LintIssue(
                rule="hook_no_greeting",
                severity="fail",
                message=(
                    "Hook contains a greeting / channel intro; no greeting, welcome, "
                    "or intro is allowed -- the hook must open with proof (0-5s)."
                ),
                location="hook",
            )
        )

    # -- Rule 8: hook must prove the title's promise in the first 30s.
    if not sidecar.hook_first_30s_has_proof:
        issues.append(
            LintIssue(
                rule="hook_proof",
                severity="fail",
                message=(
                    "Hook has no proof in the first 30 seconds; 0-5s must deliver one "
                    "concrete example/fact/scene line proving the title's promise."
                ),
                location="hook",
            )
        )

    return issues


def retention_lint_passed(issues: list[LintIssue]) -> bool:
    """True iff there are no ``fail``-severity issues (warnings pass)."""
    return not any(issue.severity == "fail" for issue in issues)
