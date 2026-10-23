"""Style Lab W2 — spec seam + content/script + SEO wiring.

Pins the W2 contract against W1's real module
(``app.integration.style_profile``, schema ``style_profile.v1``):

* ``ProjectSpec.style_profile_path`` parses (default ``None``) and
  ``extra="forbid"`` still rejects unknown fields;
* deterministic :func:`build_content` with a resolved profile:
  chapter count targets ``chapters_target`` (+-1), the overall word
  target is ``speech_wpm x target_duration_s``, per-chapter budgets
  derive from ``speech_wpm x avg_chapter_s`` with the hook budget
  from ``speech_wpm x hook_window_s``, the title conforms to
  ``title_formula`` and the description to
  ``description_structure`` — all recorded under
  ``bundle.style_applied == {"script": ..., "seo": ...}``;
* style does NOT bypass Gate A: the styled bundle still passes
  ``run_gate_a`` on its own merits;
* live Gemini path: the style-instruction block (format only, with
  the explicit originality line) is appended to the outline and
  chapter prompts, and the bundle records ``style_applied``;
* no profile resolving -> behaviour unchanged (``style_applied`` is
  ``None``, the historical title/description shapes are kept);
* an explicitly configured but missing profile file fails loud
  (R4-003) instead of silently falling back to unstyled output.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from pydantic import ValidationError

from app.integration.content import (
    build_content,
    build_content_live,
    run_gate_a,
)
from app.integration.models import ProjectSpec
from app.integration.style_profile import StyleProfile, save_profile
from app.niches.builtins import get_profile
from app.providers.llm.models import LLMResult, UsageBuckets

TOPIC = "Samundar ki Gehrai ke Raaz"
CHANNEL = "style-test-channel"


@pytest.fixture(autouse=True)
def _no_env_profile(monkeypatch):
    """Isolate every test from a deployment-level env profile."""
    monkeypatch.delenv("YTA_STYLE_PROFILE_PATH", raising=False)


def _profile_data(**overrides) -> dict:
    data = dict(
        channel_id=CHANNEL,
        video_count=3,
        duration={"target_s": 240.0, "median_s": 240.0},
        speech={"wpm_median": 150.0, "language": "roman_urdu"},
        hook={"window_s": 12.0, "types": ["question", "bold_claim"]},
        chapters={"target_count": 5, "avg_chapter_s": 48.0},
        visuals={
            "stock_ratio": 0.8,
            "ai_ratio": 0.2,
            "avg_shot_len_s": 6.0,
            "cuts_per_min": 10.0,
        },
        captions={"burned_in": True},
        music={"expected": True},
        thumbnail={
            "layout_class": "face_right_text_left",
            "text_words_min": 3,
            "text_words_max": 4,
            "face_expected": True,
            "palette_class": "high_contrast_warm",
        },
        title_formula={
            "len_min": 40,
            "len_max": 60,
            "number_rate": 0.8,
            "bracket_rate": 0.2,
            "caps_rate": 0.1,
            "keyword_position": "start",
        },
        description_structure={
            "sections": ["intro", "chapters", "cta", "hashtags"],
            "hashtag_min": 2,
            "hashtag_max": 4,
            "link_expected": False,
        },
    )
    data.update(overrides)
    return data


def _write_profile(tmp_path, **overrides) -> str:
    profile = StyleProfile(**_profile_data(**overrides))
    return str(save_profile(profile, tmp_path / "style.json"))


def _spec(**overrides) -> ProjectSpec:
    data = dict(
        niche="facts", topic=TOPIC, minutes=4.0, channel_id=CHANNEL)
    data.update(overrides)
    return ProjectSpec(**data)


def _styled_spec(tmp_path, **profile_overrides) -> ProjectSpec:
    return _spec(style_profile_path=_write_profile(tmp_path, **profile_overrides))


# ---------------------------------------------------------------------------
# Spec seam
# ---------------------------------------------------------------------------


def test_spec_style_profile_path_defaults_none_and_parses():
    assert _spec().style_profile_path is None
    spec = _spec(style_profile_path="/tmp/some-profile.json")
    assert spec.style_profile_path == "/tmp/some-profile.json"


def test_spec_still_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        _spec(style_profile="/tmp/nope.json")


# ---------------------------------------------------------------------------
# Deterministic path — script steering
# ---------------------------------------------------------------------------


def test_deterministic_bundle_follows_profile_script_targets(tmp_path):
    spec = _styled_spec(tmp_path)
    bundle = build_content(spec, get_profile("facts"))

    # Chapter count targets chapters_target (5), within the +-1 band.
    assert abs(len(bundle.chapters) - 5) <= 1
    # Overall word target = speech_wpm x target_duration_s = 150 x 4 min.
    assert bundle.target_words == 600
    # Narration lands inside Gate A's +-25% of that target.
    words = len(bundle.narration_text.split())
    assert 600 * 0.75 <= words <= 600 * 1.25

    script = bundle.style_applied["script"]
    assert script["target_duration_s"] == 240.0
    assert script["speech_wpm"] == 150.0
    assert script["target_words"] == 600
    assert script["chapters_target"] == 5
    assert script["chapters_planned"] == len(bundle.chapters)
    # Hook budget from speech_wpm x hook_window_s = 150 x 12 s = 30 words;
    # per-chapter budgets from speech_wpm x avg_chapter_s = 120 words,
    # drift-corrected so the budgets still sum to the word target.
    assert script["hook_window_s"] == 12.0
    assert script["hook_budget_words"] == 30
    assert script["hook_types"] == ["question", "bold_claim"]
    assert script["avg_chapter_words"] == 120
    assert script["chapter_word_budgets"][0] == 30
    assert sum(script["chapter_word_budgets"]) == 600


def test_deterministic_chapter_count_expands_and_contracts(tmp_path):
    profile = get_profile("facts")  # 4-section structure template
    big = build_content(_styled_spec(tmp_path), profile)
    assert len(big.chapters) == 5  # expanded past the template's 4

    small_path = _write_profile(
        tmp_path / "small",
        chapters={"target_count": 3, "avg_chapter_s": 80.0},
    )
    small = build_content(_spec(style_profile_path=small_path), profile)
    assert abs(len(small.chapters) - 3) <= 1  # contracted to ~3


# ---------------------------------------------------------------------------
# Deterministic path — SEO conformance
# ---------------------------------------------------------------------------


def test_deterministic_title_and_description_conform(tmp_path):
    spec = _styled_spec(tmp_path)
    bundle = build_content(spec, get_profile("facts"))

    # Title formula: length in [40, 60], number included
    # (number_rate 0.8 >= 0.5), pipe separator (bracket_rate 0.2),
    # topic keyword at the start.
    assert 40 <= len(bundle.title) <= 60
    assert bundle.title.startswith(TOPIC)
    assert " | " in bundle.title
    assert re.search(r"\d", bundle.title)
    title_applied = bundle.style_applied["seo"]["title"]
    assert title_applied["number_included"] is True
    assert title_applied["separator"] == "pipe"
    assert title_applied["keyword_position"] == "start"
    assert title_applied["length"] == len(bundle.title)

    # Description structure: sections in profile order, hashtag count
    # inside [2, 4], no links (link_expected False).
    desc = bundle.description
    markers = [
        desc.index(TOPIC),                 # intro
        desc.index("Chapters:"),           # chapters
        desc.index("subscribe karein"),    # cta (profile cta_style)
        desc.index("#"),                   # hashtags
    ]
    assert markers == sorted(markers)
    hashtag_count = len(re.findall(r"#\w+", desc))
    assert 2 <= hashtag_count <= 4
    assert "http" not in desc
    desc_applied = bundle.style_applied["seo"]["description"]
    assert desc_applied["sections"] == ["intro", "chapters", "cta", "hashtags"]
    assert desc_applied["hashtag_count"] == hashtag_count
    assert desc_applied["link_included"] is False


def test_title_formula_bracket_caps_keyword_end(tmp_path):
    path = _write_profile(
        tmp_path,
        title_formula={
            "len_min": 30,
            "len_max": 90,
            "number_rate": 0.1,
            "bracket_rate": 0.9,
            "caps_rate": 0.9,
            "keyword_position": "end",
        },
        description_structure={
            "sections": ["intro", "links", "chapters", "cta", "hashtags"],
            "hashtag_min": 1,
            "hashtag_max": 3,
            "link_expected": True,
        },
    )
    bundle = build_content(_spec(style_profile_path=path), get_profile("facts"))

    # Bracket separator, topic keyword at the END, caps suffix,
    # no number (number_rate 0.1 < 0.5).
    assert bundle.title.endswith(f"[{TOPIC}]")
    assert 30 <= len(bundle.title) <= 90
    assert not re.search(r"\d", bundle.title)
    suffix = bundle.title.split("[")[0].strip()
    assert suffix and suffix == suffix.upper()
    title_applied = bundle.style_applied["seo"]["title"]
    assert title_applied["separator"] == "bracket"
    assert title_applied["caps_applied"] is True
    assert title_applied["number_included"] is False

    # Links section present because link_expected, in profile order.
    desc_applied = bundle.style_applied["seo"]["description"]
    assert desc_applied["sections"] == [
        "intro", "links", "chapters", "cta", "hashtags"]
    assert desc_applied["link_included"] is True
    assert 1 <= desc_applied["hashtag_count"] <= 3


# ---------------------------------------------------------------------------
# Gate A is not bypassed
# ---------------------------------------------------------------------------


def test_title_padding_never_overshoots_len_max(tmp_path):
    """Regression (Style Lab proof): a tight band [60, 60] with the
    pad-to-len_min loop appending whole bracketed tails produced a
    71-char title in the proof run. Padding must respect len_max."""
    path = _write_profile(
        tmp_path,
        title_formula={
            "len_min": 60,
            "len_max": 60,
            "number_rate": 0.0,
            "bracket_rate": 1.0,
            "caps_rate": 0.0,
            "keyword_position": "start",
        },
    )
    spec = _spec(topic="Chand ki Doosri Janib ke Raaz",
                 style_profile_path=path)
    bundle = build_content(spec, get_profile("facts"))
    title_applied = bundle.style_applied["seo"]["title"]
    assert len(bundle.title) <= 60
    assert title_applied["length"] == len(bundle.title) <= 60


def test_styled_bundle_still_passes_gate_a_on_its_own_merits(tmp_path):
    spec = _styled_spec(tmp_path)
    profile = get_profile("facts")
    bundle = build_content(spec, profile)
    passed, reasons = run_gate_a(bundle, profile, spec)
    assert passed, reasons


# ---------------------------------------------------------------------------
# No profile -> unchanged behaviour
# ---------------------------------------------------------------------------


def test_no_profile_behaviour_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no <cwd>/style_profiles/<channel>.json
    spec = _spec()
    profile = get_profile("facts")
    bundle = build_content(spec, profile)

    assert bundle.style_applied is None
    # Historical shapes (the pre-Style-Lab golden form): pipe title
    # built from topic + suffix, description without any hashtags.
    assert bundle.title.startswith(f"{TOPIC} | ")
    assert "#" not in bundle.description
    assert bundle.description.endswith(profile.cta_style)
    # Identical to a second build (deterministic golden stability).
    again = build_content(spec, profile)
    assert again.title == bundle.title
    assert again.description == bundle.description
    assert again.narration_text == bundle.narration_text


def test_missing_explicit_profile_fails_loud(tmp_path):
    spec = _spec(style_profile_path=str(tmp_path / "does-not-exist.json"))
    with pytest.raises(FileNotFoundError):
        build_content(spec, get_profile("facts"))


# ---------------------------------------------------------------------------
# Live Gemini path
# ---------------------------------------------------------------------------


class _FakeSecrets:
    async def get_secret(self, ref: str):
        return "a-key"


class _RecordingLLM:
    """LLMProvider fake mirroring tests/integration/test_content_live.py."""

    def __init__(self):
        self.requests = []

    async def generate(self, req) -> LLMResult:
        self.requests.append(req)
        if req.task == "outline":
            text = "Outline: hook, chapters, climax, conclusion."
            model = "gemini-3.8-flash"
        elif req.task == "script.final_pass":
            text = req.prompt.split("Draft script:\n", 1)[-1]
            model = "gemini-3.1-pro-preview"
        else:
            text = (
                "Ye chapter Samundar ki gehrai ke baare mein ek hairan "
                "kun baat batata hai jo samajh ko gehra karti hai aur "
                "aage ki kahani ke liye sawal khara karti hai."
            )
            model = "gemini-3.8-flash"
        return LLMResult(
            text=text,
            provider="gemini",
            model=model,
            tier=req.tier,
            usage=UsageBuckets(input_uncached=100, output=40),
            cost_usd=0.001,
            prompt_version=req.prompt_version,
        )


def test_live_path_prompts_carry_style_block_and_bundle_records_it(tmp_path):
    spec = _styled_spec(tmp_path)
    llm = _RecordingLLM()
    bundle = asyncio.run(
        build_content_live(
            spec, get_profile("facts"),
            secret_provider=_FakeSecrets(), llm=llm,
        )
    )
    assert bundle.llm_path == "gemini-live"
    # Style plan drove the live generation: 5 chapters, 600-word target.
    assert len(bundle.chapters) == 5
    assert bundle.target_words == 600

    outline = [r for r in llm.requests if r.task == "outline"]
    chapters = [r for r in llm.requests if r.task == "script.chapter"]
    assert outline and len(chapters) == 5
    for req in outline + chapters:
        assert "STYLE PROFILE" in req.prompt
        assert "question" in req.prompt  # profile hook types
        # The originality line: format only, never copied content.
        assert "ORIGINALITY (mandatory)" in req.prompt
        assert "Never copy" in req.prompt

    # style_applied recorded on the live bundle too.
    assert bundle.style_applied["script"]["chapters_planned"] == 5
    assert bundle.style_applied["script"]["target_words"] == 600
    assert bundle.style_applied["seo"]["title"]["keyword_position"] == "start"
    desc_applied = bundle.style_applied["seo"]["description"]
    assert desc_applied["sections"] == ["intro", "chapters", "cta", "hashtags"]
    assert 2 <= desc_applied["hashtag_count"] <= 4
    assert 40 <= len(bundle.title) <= 60


def test_live_path_without_profile_has_no_style_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    llm = _RecordingLLM()
    bundle = asyncio.run(
        build_content_live(
            _spec(), get_profile("facts"),
            secret_provider=_FakeSecrets(), llm=llm,
        )
    )
    assert bundle.llm_path == "gemini-live"
    assert bundle.style_applied is None
    assert llm.requests
    assert all("STYLE PROFILE" not in r.prompt for r in llm.requests)
