"""W5: style-profile report visibility + gate-integrity proof.

Covers the Style Lab mini-round's produce-side contract:

* when a run resolves a style profile, ``report.md`` gains a
  ``## Style Profile Applied`` section listing the profile identity
  (schema version, channel, provenance source count) and the
  per-stage applied parameters aggregated DEFENSIVELY from the stage
  results (script / seo from the content bundle, visuals from the
  visuals result, thumbnail from the assemble result; a stage that
  recorded nothing renders "no style parameters applied");
* when no profile is used, NOTHING is appended — reports for
  non-style runs stay byte-identical;
* GATE INTEGRITY: a style profile is NOT a copying licence and
  changes nothing about gate outcomes — Gate A still executes with a
  profile applied, and a script Gate A rejects still fails the run;
* a profile file poisoned with a verbatim competitor title field is
  rejected at load (W1's ``extra="forbid"`` schema) and the produce
  run fails loud instead of silently ignoring the broken profile
  (R4-003).

Stage functions are monkeypatched with contract-shaped fakes, exactly
like ``test_produce_glue`` — no network, no TTS, no ffmpeg.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.integration import produce
from app.integration.models import ProductionResult, ProjectSpec
from app.integration.style_profile import StyleProfile, load_profile, save_profile

CHANNEL_ID = "style-chan-1"
COMPETITOR_TITLE = "Top 10 Facts | You Won't Believe"


# ---------------------------------------------------------------------------
# Helpers (same monkeypatch contract as test_produce_glue)
# ---------------------------------------------------------------------------

def _patch_stages(monkeypatch, fakes, gate=(True, []), calls=None):
    def rec(name, ret):
        def _fn(*args):
            if calls is not None:
                calls.append(name)
            return ret
        return _fn

    monkeypatch.setattr(produce, "build_content", rec("content", fakes.bundle))
    monkeypatch.setattr(produce, "run_gate_a", rec("gate_a", gate))
    monkeypatch.setattr(produce, "synthesize_voice", rec("voice", fakes.voice))
    monkeypatch.setattr(
        produce, "plan_and_generate_visuals", rec("visuals", fakes.visuals)
    )
    monkeypatch.setattr(produce, "assemble_project", rec("assemble", fakes.assemble))
    monkeypatch.setattr(produce, "finalize_publish", rec("publish", fakes.publish))


def _spec(**overrides) -> ProjectSpec:
    data = dict(niche="facts", topic="Ocean depth secrets", minutes=1.0)
    data.update(overrides)
    return ProjectSpec(**data)


def _write_style_profile(tmp_path: Path, channel_id: str = CHANNEL_ID) -> Path:
    """A schema-valid ``style_profile.v1`` fixture with 2 provenance
    sources, written through W1's own ``save_profile``."""
    profile = StyleProfile(
        channel_id=channel_id,
        video_count=2,
        provenance={
            "source_asset_ids": ["comp-1", "comp-2"],
            "source_sha256": ["a" * 64, "b" * 64],
        },
        no_copy_manifest={
            "never_reuse_asset_ids": ["comp-1", "comp-2"],
            "never_reuse_sha256": ["a" * 64, "b" * 64],
            "contains_competitor_assets": False,
        },
    )
    return save_profile(profile, tmp_path / "style_profile.json")


def _styled_fakes(stage_fakes):
    """Fakes whose stages recorded contract-shaped ``style_applied``."""
    stage_fakes.bundle.style_applied = {
        "script": {"hook_window_s": 8.0, "chapters_target": 6},
        "seo": {"keyword_position": "start", "title_len_min": 30},
    }
    stage_fakes.visuals.style_applied = {
        "visuals": {"stock_ratio": 0.67, "avg_shot_len_s": 10.0},
    }
    stage_fakes.assemble.style_applied = {
        "thumbnail": {"layout_class": "face_right_text_left"},
    }
    return stage_fakes


@pytest.fixture(autouse=True)
def _no_env_style_profile(monkeypatch):
    """The resolver's env fallback must never leak into these tests."""
    monkeypatch.delenv("YTA_STYLE_PROFILE_PATH", raising=False)


# ---------------------------------------------------------------------------
# (a) Style run: section rendered with aggregated params
# ---------------------------------------------------------------------------

def test_style_section_rendered_with_aggregated_params(
    monkeypatch, stage_fakes, tmp_path
):
    profile_path = _write_style_profile(tmp_path)
    _patch_stages(monkeypatch, _styled_fakes(stage_fakes))
    spec = _spec(channel_id=CHANNEL_ID, style_profile_path=str(profile_path))
    result = produce.produce_video(spec, tmp_path / "out")

    assert isinstance(result, ProductionResult)
    report = Path(result.report_md).read_text(encoding="utf-8")

    # Section + profile identity.
    assert "## Style Profile Applied" in report
    assert "- Schema version: style_profile.v1" in report
    assert f"- Channel ID: {CHANNEL_ID}" in report
    assert "- Provenance source count: 2" in report
    assert str(profile_path) in report

    # Per-stage subsections with the aggregated parameters.
    for heading in ("### Script", "### SEO", "### Visuals", "### Thumbnail"):
        assert heading in report
    assert "- hook_window_s: 8.0" in report
    assert "- chapters_target: 6" in report
    assert "- keyword_position: start" in report
    assert "- title_len_min: 30" in report
    assert "- stock_ratio: 0.67" in report
    assert "- avg_shot_len_s: 10.0" in report
    assert "- layout_class: face_right_text_left" in report
    assert "no style parameters applied" not in report

    # ProductionResult exposes the summary via its existing
    # paths_used provenance dict (models.py carries no metadata field).
    assert "style_profile.v1" in result.paths_used["style_profile"]
    assert CHANNEL_ID in result.paths_used["style_profile"]


def test_style_section_missing_stages_show_not_applied(
    monkeypatch, stage_fakes, tmp_path
):
    """Only the content bundle recorded params (script only): every
    other stage subsection renders the not-applied line instead of
    crashing or inventing values."""
    profile_path = _write_style_profile(tmp_path)
    stage_fakes.bundle.style_applied = {"script": {"hook_window_s": 5.0}}
    _patch_stages(monkeypatch, stage_fakes)
    spec = _spec(channel_id=CHANNEL_ID, style_profile_path=str(profile_path))
    result = produce.produce_video(spec, tmp_path / "partial")

    report = Path(result.report_md).read_text(encoding="utf-8")
    assert "## Style Profile Applied" in report
    assert "- hook_window_s: 5.0" in report
    # SEO, Visuals and Thumbnail recorded nothing.
    assert report.count("no style parameters applied") == 3


def test_append_style_section_aggregates_defensively(tmp_path):
    """Unit-pin the appender's defensive aggregation: live stage
    objects parked in ``data``, a single-stage raw dict that does not
    use its stage key (taken whole), and missing stages."""
    report_path = tmp_path / "report.md"
    report_path.write_text("# Production Report\n", encoding="utf-8")
    data = {
        "style_profile": {
            "schema_version": "style_profile.v1",
            "channel_id": CHANNEL_ID,
            "source_count": 3,
            "profile_path": "/profiles/chan.json",
        },
        "bundle": SimpleNamespace(
            style_applied={"script": {"hook_types": ["question"]}}),
        # No "visuals" key in the raw dict -> whole dict is the params.
        "visuals": SimpleNamespace(style_applied={"stock_ratio": 0.5}),
        # No "assemble" entry at all -> thumbnail is not applied.
    }
    produce._append_style_section(report_path, data)
    text = report_path.read_text(encoding="utf-8")
    assert "## Style Profile Applied" in text
    assert "- Provenance source count: 3" in text
    assert "hook_types: question" in text
    assert "- stock_ratio: 0.5" in text
    assert text.count("no style parameters applied") == 2  # seo + thumbnail

    # Idempotent: a second call must not duplicate the section.
    produce._append_style_section(report_path, data)
    assert report_path.read_text(encoding="utf-8").count(
        "## Style Profile Applied") == 1


# ---------------------------------------------------------------------------
# (b) No profile: nothing appended, byte-identical reports
# ---------------------------------------------------------------------------

def test_no_profile_run_has_no_style_section(monkeypatch, stage_fakes, tmp_path):
    _patch_stages(monkeypatch, stage_fakes)
    result = produce.produce_video(_spec(), tmp_path / "plain")
    report = Path(result.report_md).read_text(encoding="utf-8")
    assert "## Style Profile Applied" not in report
    assert "style_profile" not in result.paths_used


def test_append_style_section_without_profile_is_byte_identical(tmp_path):
    report_path = tmp_path / "report.md"
    original = "# Production Report\n\n- Gate A passed: True\n"
    report_path.write_bytes(original.encode("utf-8"))
    produce._append_style_section(report_path, {"style_profile": None})
    produce._append_style_section(report_path, {})
    assert report_path.read_bytes() == original.encode("utf-8")


# ---------------------------------------------------------------------------
# (c) GATE INTEGRITY: the profile changes nothing about gate outcomes
# ---------------------------------------------------------------------------

def test_gate_a_still_blocks_auto_mode_with_style_profile(
    monkeypatch, stage_fakes, tmp_path
):
    profile_path = _write_style_profile(tmp_path)
    calls: list[str] = []
    _patch_stages(
        monkeypatch,
        _styled_fakes(stage_fakes),
        gate=(False, ["RED claim somewhere"]),
        calls=calls,
    )
    spec = _spec(
        channel_id=CHANNEL_ID,
        style_profile_path=str(profile_path),
        publish_mode="auto",
    )
    result = produce.produce_video(spec, tmp_path / "blocked")

    # Gate A executed and its verdict decided the run — the applied
    # style profile did not soften, skip or bypass it.
    assert calls == ["content", "gate_a"]
    assert result.gate_a_passed is False
    assert result.gate_b_passed is False
    assert result.publish_state == "blocked_gate_a"
    assert result.final_mp4 is None
    assert any("Gate A FAILED" in w for w in result.warnings)

    # The blocked run's report still shows what style was applied to
    # the content stage before the gate stopped the run.
    report = Path(result.report_md).read_text(encoding="utf-8")
    assert "## Style Profile Applied" in report
    assert "- hook_window_s: 8.0" in report


def test_gate_a_failure_with_profile_continues_only_in_review_first(
    monkeypatch, stage_fakes, tmp_path
):
    profile_path = _write_style_profile(tmp_path)
    _patch_stages(
        monkeypatch,
        _styled_fakes(stage_fakes),
        gate=(False, ["lint issue"]),
    )
    spec = _spec(
        channel_id=CHANNEL_ID,
        style_profile_path=str(profile_path),
        publish_mode="review_first",
    )
    result = produce.produce_video(spec, tmp_path / "review")
    assert result.gate_a_passed is False
    assert any("review_first" in w for w in result.warnings)
    # Same review_first-only continuation as an unstyled run.
    assert result.final_mp4 == stage_fakes.assemble.final_mp4


# ---------------------------------------------------------------------------
# (d) No-copy / schema integrity at load + fail-loud resolution
# ---------------------------------------------------------------------------

def test_poisoned_profile_with_verbatim_title_field_rejected_at_load(tmp_path):
    """A profile JSON carrying a verbatim competitor title field is
    not a valid StyleProfile: W1's schema is ``extra="forbid"`` and a
    profile stores patterns only, never competitor text."""
    profile_path = _write_style_profile(tmp_path)
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    payload["competitor_title"] = COMPETITOR_TITLE
    profile_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_profile(profile_path)


def test_poisoned_profile_fails_the_run_loud(monkeypatch, stage_fakes, tmp_path):
    """The same poisoned file reached through produce must error the
    run visibly at resolve time (R4-003) — never be swallowed into an
    unstyled silent fallback."""
    profile_path = _write_style_profile(tmp_path)
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    payload["seo_title_text"] = COMPETITOR_TITLE
    profile_path.write_text(json.dumps(payload), encoding="utf-8")

    _patch_stages(monkeypatch, stage_fakes)
    spec = _spec(channel_id=CHANNEL_ID, style_profile_path=str(profile_path))
    with pytest.raises(ValidationError):
        produce.produce_video(spec, tmp_path / "poisoned")


def test_explicit_missing_profile_path_fails_loud(
    monkeypatch, stage_fakes, tmp_path
):
    _patch_stages(monkeypatch, stage_fakes)
    spec = _spec(
        channel_id=CHANNEL_ID,
        style_profile_path=str(tmp_path / "does-not-exist.json"),
    )
    with pytest.raises(FileNotFoundError):
        produce.produce_video(spec, tmp_path / "missing")
