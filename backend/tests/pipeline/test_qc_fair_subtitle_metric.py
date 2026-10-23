"""R4-005 — fair subtitle metric wired into production QC (Round 5).

The transliteration-aware fair metric
(:func:`app.pipeline.subtitles.validation.script_match_ratio_fair`)
existed since Round 4 but production QC reported only the strict,
cross-script ``script_match_ratio`` (a Roman Urdu reference vs
Urdu-script Whisper captions scores ~0.0 by construction).  These tests
pin the Round 5 wiring:

* ``check_script_match`` / ``SubtitleResult.metadata`` /
  the assemble stage's QC summary carry BOTH scores, each paired with
  its metric name — never a bare, unlabelled number.
* The fair score is report / WARN context ONLY
  (``SUBTITLE_SCRIPT_MATCH_GATE_POLICY == "report_only"``): ``passed``
  is still decided by the strict score against the unchanged 0.98
  threshold, even when the fair score is a perfect 1.0.
* Gate B outcomes are unchanged: ``classify_qc`` / ``evaluate_gate_b``
  read QC verdict + reason-code flags only; subtitle scores smuggled
  into a QC token's evidence change nothing, and no subtitle flag was
  added to ``MINOR_QC_FLAGS``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.pipeline.gates.aggregator import evaluate_gate_b, verify_gate_b  # noqa: E402
from app.pipeline.gates.conditional import MINOR_QC_FLAGS, classify_qc  # noqa: E402
from app.pipeline.gates.tokens import (  # noqa: E402
    GATE_COPYRIGHT,
    GATE_POLICY_SIMILARITY,
    GATE_QC_TECHNICAL,
    Verdict,
    compute_manifest_hash,
    issue_token,
)
from app.pipeline.subtitles import build_subtitles  # noqa: E402
from app.pipeline.subtitles.models import (  # noqa: E402
    SOURCE_WHISPER,
    CaptionsProfile,
    TranscriptSegment,
    WordTiming,
)
from app.pipeline.subtitles.validation import (  # noqa: E402
    FAIR_SCRIPT_MATCH_METRIC,
    MIN_SCRIPT_MATCH_BY_PROFILE,
    STRICT_SCRIPT_MATCH_METRIC,
    SUBTITLE_SCRIPT_MATCH_GATE_POLICY,
    check_script_match,
    script_match_ratio,
    script_match_ratio_fair,
)

# Same real Round 3 line pair as test_subtitle_fair_metric.py.
ROMAN_LINE = (
    "Kya aap jante hain ke hamari zameen ka sab se gehra raaz "
    "pani ki gehrayion mein chupa hai?"
)
URDU_LINE = (
    "کیا آپ جانتے ہیں کہ ہماری زمین کا سب سے گہرا راز "
    "پانی کی گہرائیوں میں چھپا ہے؟"
)


# ---------------------------------------------------------------------------
# Metric naming + policy constants
# ---------------------------------------------------------------------------


def test_metric_names_match_the_functions_they_label():
    assert STRICT_SCRIPT_MATCH_METRIC == script_match_ratio.__name__
    assert FAIR_SCRIPT_MATCH_METRIC == script_match_ratio_fair.__name__
    assert SUBTITLE_SCRIPT_MATCH_GATE_POLICY == "report_only"


def test_thresholds_unchanged():
    # The 0.98 bar predates Round 5 and must not move as a side effect
    # of reporting the fair score.
    assert set(MIN_SCRIPT_MATCH_BY_PROFILE.values()) == {0.98}


# ---------------------------------------------------------------------------
# check_script_match: both scores, strict still decides `passed`
# ---------------------------------------------------------------------------


def test_check_script_match_carries_both_named_scores():
    report = check_script_match(ROMAN_LINE, URDU_LINE, CaptionsProfile.ROMAN_URDU)
    assert report.ratio == pytest.approx(script_match_ratio(ROMAN_LINE, URDU_LINE))
    assert report.ratio_fair == pytest.approx(
        script_match_ratio_fair(ROMAN_LINE, URDU_LINE)
    )
    assert report.ratio < 0.1
    assert report.ratio_fair > 0.55
    metrics = report.metrics_dict()
    assert metrics["strict"] == {
        "metric": "script_match_ratio",
        "score": report.ratio,
    }
    assert metrics["fair"] == {
        "metric": "script_match_ratio_fair",
        "score": report.ratio_fair,
    }
    assert metrics["gate_policy"] == "report_only"
    json.dumps(metrics)  # QC reports are JSON — must stay serialisable


def test_fair_score_never_decides_passed():
    # "Kya hai?" vs its faithful Urdu-script rendering: the fair metric
    # scores a perfect 1.0 while the strict metric scores 0.0.  The
    # gate result must follow the STRICT score — reporting the fair
    # score must not silently raise the effective pass rate.
    report = check_script_match("Kya hai?", "کیا ہے؟", CaptionsProfile.ROMAN_URDU)
    assert report.ratio == pytest.approx(0.0)
    assert report.ratio_fair == pytest.approx(1.0)
    assert report.threshold == pytest.approx(0.98)
    assert report.passed is False


def test_identical_text_scores_one_under_both_and_passes():
    report = check_script_match(ROMAN_LINE, ROMAN_LINE, CaptionsProfile.ROMAN_URDU)
    assert report.ratio == pytest.approx(1.0)
    assert report.ratio_fair == pytest.approx(1.0)
    assert report.passed is True


# ---------------------------------------------------------------------------
# Subtitle service: metadata + WARN context carry both scores
# ---------------------------------------------------------------------------


def _urdu_whisper_segments(extra_word: str = "واقعی") -> list[TranscriptSegment]:
    # One word MORE than the Roman script's token count, so cue wording
    # stays the Whisper (Urdu-script) wording instead of being replaced
    # by the script wording — the production cross-script situation.
    tokens = (URDU_LINE + f" {extra_word}").split()
    words = []
    cursor = 0.0
    for token in tokens:
        words.append(
            WordTiming(token, cursor, cursor + 0.4, source=SOURCE_WHISPER)
        )
        cursor += 0.45
    return [
        TranscriptSegment(
            text=" ".join(tokens),
            start=0.0,
            end=cursor,
            words=tuple(words),
            source=SOURCE_WHISPER,
        )
    ]


def test_build_subtitles_metadata_carries_both_named_scores():
    result = build_subtitles(
        transcription_segments=_urdu_whisper_segments(),
        script_text=ROMAN_LINE,
        profile=CaptionsProfile.ROMAN_URDU,
    )
    assert result.script_match is not None
    assert result.script_match_ratio is not None and result.script_match_ratio < 0.1
    assert result.script_match_ratio_fair is not None
    assert result.script_match_ratio_fair > 0.5

    metadata = result.metadata
    assert metadata["script_match_ratio"] == pytest.approx(result.script_match_ratio)
    assert metadata["script_match_ratio_fair"] == pytest.approx(
        result.script_match_ratio_fair
    )
    metrics = metadata["script_match_metrics"]
    assert metrics["strict"]["metric"] == "script_match_ratio"
    assert metrics["strict"]["score"] == pytest.approx(result.script_match_ratio)
    assert metrics["fair"]["metric"] == "script_match_ratio_fair"
    assert metrics["fair"]["score"] == pytest.approx(result.script_match_ratio_fair)
    # Strict still decides the pass flag.
    assert metadata["script_match_passed"] is False
    json.dumps(metadata)


def test_script_match_warning_names_both_metrics():
    result = build_subtitles(
        transcription_segments=_urdu_whisper_segments(),
        script_text=ROMAN_LINE,
        profile=CaptionsProfile.ROMAN_URDU,
    )
    match_warnings = [w for w in result.warnings if "script-match" in w]
    assert len(match_warnings) == 1
    warning = match_warnings[0]
    assert "script_match_ratio" in warning
    assert "script_match_ratio_fair" in warning
    assert "report-only" in warning


# ---------------------------------------------------------------------------
# Assemble stage: the production QC summary carries both scores
# ---------------------------------------------------------------------------


class _FakeTranscriber:
    def __init__(self, segments):
        self._segments = segments
        self.last_provenance = SOURCE_WHISPER
        self.last_error = None

    def transcribe(self, audio_path, language=None, **kwargs):
        return list(self._segments)


class _FakeRenderer:
    def __init__(self, out_dir, encoder_choice=None):
        self.out_dir = Path(out_dir)

    def render_all(self, specs):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / "seg_0001.ts"
        path.write_bytes(b"ts")
        return [path]


def test_assemble_qc_summary_subtitles_carry_both_named_scores(
    monkeypatch, tmp_path
):
    from app.integration import assemble_stage

    monkeypatch.setattr(
        assemble_stage, "probe_encoder", lambda: SimpleNamespace(encoder="fake")
    )
    monkeypatch.setattr(assemble_stage, "SegmentRenderer", _FakeRenderer)
    monkeypatch.setattr(
        assemble_stage,
        "render_draft",
        lambda segment_paths, out, audio=None, encoder_choice=None: Path(
            out
        ).write_bytes(b"mp4"),
    )
    monkeypatch.setattr(assemble_stage, "probe_duration", lambda p: 9.0)
    monkeypatch.setattr(
        assemble_stage,
        "run_technical_qc",
        lambda *a, **k: SimpleNamespace(
            verdict=Verdict.PASS,
            loudness=SimpleNamespace(
                integrated_lufs=-14.0, true_peak_dbtp=-1.0, lra_lu=2.0,
                source="fake",
            ),
            checks=[],
            detections={},
        ),
    )
    thumbs = []
    for i in range(3):
        t = tmp_path / f"variant_{i}.png"
        t.write_bytes(b"png")
        thumbs.append(
            SimpleNamespace(
                path=str(t), layout_id="fake", headline="Ocean",
                legibility=None, warnings=[],
            )
        )
    monkeypatch.setattr(
        assemble_stage, "generate_variants",
        lambda *a, **k: SimpleNamespace(variants=thumbs),
    )
    fake = _FakeTranscriber(_urdu_whisper_segments())
    monkeypatch.setattr(
        assemble_stage, "create_transcriber", lambda *a, **k: fake
    )

    audio = tmp_path / "master.wav"
    audio.write_bytes(b"fake-audio")
    bundle = SimpleNamespace(
        script_text=ROMAN_LINE, title="Ocean Test", thumbnail_text="Ocean"
    )
    voice = SimpleNamespace(mixed_master_wav=str(audio), duration_s=9.0)
    profile = SimpleNamespace(language="roman_urdu")
    ctx = SimpleNamespace(
        pack_dir=str(tmp_path / "pack"), work_dir=str(tmp_path / "work")
    )

    result = assemble_stage.assemble_project(bundle, voice, [], profile, ctx)

    subtitles = result.qc_summary["subtitles"]
    assert subtitles["script_match_ratio"] < 0.1
    assert subtitles["script_match_ratio_fair"] > 0.5
    metrics = subtitles["script_match_metrics"]
    assert metrics["strict"] == {
        "metric": "script_match_ratio",
        "score": subtitles["script_match_ratio"],
    }
    assert metrics["fair"] == {
        "metric": "script_match_ratio_fair",
        "score": subtitles["script_match_ratio_fair"],
    }
    # The notes provenance is the same block the QC summary embeds.
    assert result.notes["subtitle_provenance"] == subtitles
    json.dumps(result.qc_summary)


# ---------------------------------------------------------------------------
# Pack report: report.md section renders both named scores
# ---------------------------------------------------------------------------


def test_report_md_subtitle_metrics_section(tmp_path):
    from app.integration.produce import _append_subtitle_metrics_section

    report = tmp_path / "report.md"
    report.write_text("# Production Report\n", encoding="utf-8")
    data = {
        "qc_summary": {
            "subtitles": {
                "script_match_ratio": 0.0,
                "script_match_ratio_fair": 0.6486,
                "script_match_metrics": {
                    "strict": {"metric": "script_match_ratio", "score": 0.0},
                    "fair": {
                        "metric": "script_match_ratio_fair",
                        "score": 0.6486,
                    },
                },
            }
        }
    }
    _append_subtitle_metrics_section(report, data)
    text = report.read_text(encoding="utf-8")
    assert "## Subtitle Script Match" in text
    assert "script_match_ratio" in text
    assert "script_match_ratio_fair" in text
    assert "0.6486" in text
    assert "report_only" in text
    # Idempotent: a second append does not duplicate the section.
    _append_subtitle_metrics_section(report, data)
    assert report.read_text(encoding="utf-8").count("## Subtitle Script Match") == 1


# ---------------------------------------------------------------------------
# Gate B: outcomes unchanged — subtitle scores are not an input
# ---------------------------------------------------------------------------

_HASH = compute_manifest_hash({"video": "r4005-test", "version": 1})


def _tokens(qc_verdict, qc_codes=(), subtitle_evidence=None):
    evidence = {"subtitles": subtitle_evidence} if subtitle_evidence else {}
    return [
        issue_token(
            GATE_QC_TECHNICAL, qc_verdict, _HASH,
            reason_codes=qc_codes, evidence=evidence,
        ),
        issue_token(GATE_COPYRIGHT, Verdict.PASS, _HASH),
        issue_token(GATE_POLICY_SIMILARITY, Verdict.PASS, _HASH),
    ]


@pytest.mark.parametrize(
    "evidence",
    [
        {"script_match_ratio": 0.0, "script_match_ratio_fair": 0.0},
        {"script_match_ratio": 0.0, "script_match_ratio_fair": 1.0},
        {"script_match_ratio": 1.0, "script_match_ratio_fair": 1.0},
    ],
)
def test_gate_b_pass_path_ignores_subtitle_evidence(evidence):
    tokens = _tokens(Verdict.PASS, subtitle_evidence=evidence)
    assert verify_gate_b(tokens, _HASH).allowed is True
    policy = evaluate_gate_b(tokens, _HASH)
    assert policy.allowed is True
    assert policy.outcome == "pass"


@pytest.mark.parametrize("fair", [0.0, 0.6486, 1.0])
def test_gate_b_conditional_ignores_fair_score(fair):
    # A perfect fair score must NOT promote a minor-only WARN to a
    # pass, and a zero fair score must NOT harden it into a block:
    # the conditional flow is exactly the pre-Round-5 one.
    evidence = {"script_match_ratio": 0.0, "script_match_ratio_fair": fair}
    tokens = _tokens(
        Verdict.WARN, ("BLACK_SEGMENT_BRIEF",), subtitle_evidence=evidence
    )
    pending = evaluate_gate_b(
        tokens, _HASH, mode="review_first", has_recorded_approval=False
    )
    assert pending.allowed is False
    assert pending.outcome == "conditional"
    assert pending.decision.outcome == "conditional_pending_approval"

    approved = evaluate_gate_b(
        tokens, _HASH, mode="review_first", has_recorded_approval=True
    )
    assert approved.allowed is True
    assert approved.decision.outcome == "conditional_approved"

    auto = evaluate_gate_b(
        tokens, _HASH, mode="auto", has_recorded_approval=True
    )
    assert auto.allowed is False
    assert auto.decision.outcome == "blocked_full_auto"


def test_no_subtitle_flag_added_to_minor_class():
    assert MINOR_QC_FLAGS == frozenset(
        {"BLACK_SEGMENT_BRIEF", "FREEZE_DECLARED_STILL"}
    )
    # A hypothetical subtitle reason code is NOT minor: a WARN carrying
    # only it still fails closed to block, as before Round 5.
    result = classify_qc(Verdict.WARN, ["SUBTITLE_SCRIPT_MATCH_LOW"])
    assert result.outcome == "block"
    assert result.non_minor_flags == ("SUBTITLE_SCRIPT_MATCH_LOW",)
