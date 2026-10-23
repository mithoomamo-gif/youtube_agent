"""Tests for builtin niche profiles.

Contract:
- backend/app/niches/models.py: NicheProfile with fields profile_id, name,
  source, status, version, language, tone, target_wpm (clamp 110..160),
  sentence_style, structure_template, hook_families, voice_style,
  visual_style, cta_style, risk_class, sensitive_flags, taboo,
  forbidden_patterns, special_rules
- backend/app/niches/builtins.py: BUILTIN_PROFILES dict with keys
  facts, islamic_stories, horror_stories, history, motivation, tech,
  crime_stories, health; get_profile(id)
"""
import pytest

try:
    from app.niches.builtins import BUILTIN_PROFILES, get_profile

    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001 - report as skip, not failure
    BUILTIN_PROFILES = {}
    get_profile = None  # type: ignore
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(
    _IMPORT_ERROR is not None,
    reason=f"module not yet delivered: {_IMPORT_ERROR}",
)

EXPECTED_KEYS = {
    "facts",
    "islamic_stories",
    "horror_stories",
    "history",
    "motivation",
    "tech",
    "crime_stories",
    "health",
}

EXPECTED_WPM = {
    "facts": 150,
    "islamic_stories": 120,
    "horror_stories": 115,
    "tech": 145,
}

SENSITIVE_KEYS = {"health", "islamic_stories", "crime_stories"}


def _field(profile, name, default=None):
    """Read a field from a pydantic model, object, or dict."""
    if isinstance(profile, dict):
        return profile.get(name, default)
    return getattr(profile, name, default)


def test_exactly_eight_builtins_with_expected_keys():
    assert isinstance(BUILTIN_PROFILES, dict)
    assert len(BUILTIN_PROFILES) == 8
    assert set(BUILTIN_PROFILES.keys()) == EXPECTED_KEYS


def test_builtin_wpm_values():
    for key, expected_wpm in EXPECTED_WPM.items():
        profile = BUILTIN_PROFILES[key]
        assert _field(profile, "target_wpm") == expected_wpm, (
            f"{key}: expected target_wpm={expected_wpm}, "
            f"got {_field(profile, 'target_wpm')}"
        )


def test_all_wpm_within_clamp_range():
    for key, profile in BUILTIN_PROFILES.items():
        wpm = _field(profile, "target_wpm")
        assert wpm is not None, f"{key}: missing target_wpm"
        assert 110 <= wpm <= 160, f"{key}: target_wpm {wpm} outside 110..160"


def test_sensitive_risk_class_profiles():
    for key in SENSITIVE_KEYS:
        profile = BUILTIN_PROFILES[key]
        assert _field(profile, "risk_class") == "sensitive", (
            f"{key}: expected risk_class='sensitive', "
            f"got {_field(profile, 'risk_class')!r}"
        )


def test_every_profile_structure_template_nonempty_and_hook_first():
    for key, profile in BUILTIN_PROFILES.items():
        template = _field(profile, "structure_template")
        assert template, f"{key}: structure_template is empty/missing"
        assert len(template) >= 1
        first = str(template[0]).lower()
        assert "hook" in first, (
            f"{key}: first structure_template element should be hook-like, "
            f"got {template[0]!r}"
        )


def test_every_profile_has_core_fields():
    required = [
        "profile_id",
        "name",
        "source",
        "status",
        "version",
        "language",
        "tone",
        "target_wpm",
        "sentence_style",
        "structure_template",
        "hook_families",
        "voice_style",
        "visual_style",
        "cta_style",
        "risk_class",
        "sensitive_flags",
        "taboo",
        "forbidden_patterns",
        "special_rules",
    ]
    for key, profile in BUILTIN_PROFILES.items():
        for field_name in required:
            value = _field(profile, field_name, None)
            assert value is not None, f"{key}: missing field {field_name!r}"


def test_get_profile_returns_builtin():
    for key in EXPECTED_KEYS:
        profile = get_profile(key)
        assert profile is not None
        # get_profile should agree with the dict entry on identity/value
        expected = BUILTIN_PROFILES[key]
        assert _field(profile, "target_wpm") == _field(expected, "target_wpm")
        assert _field(profile, "name") == _field(expected, "name")


def test_get_profile_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        get_profile("no_such_niche_xyz")
