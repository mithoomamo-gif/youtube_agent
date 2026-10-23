"""Tests for the 'Other' / custom niche builder.

Contract:
- backend/app/niches/builder.py:
  build_other_profile(name, idea, llm_result=None) -> NicheProfile
  with source="other", status="provisional";
  ValueError if idea < 10 chars or name < 2 chars.
"""
import pytest

try:
    from app.niches.builder import build_other_profile

    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    build_other_profile = None  # type: ignore
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(
    _IMPORT_ERROR is not None,
    reason=f"module not yet delivered: {_IMPORT_ERROR}",
)


def _field(profile, name, default=None):
    if isinstance(profile, dict):
        return profile.get(name, default)
    return getattr(profile, name, default)


def _lower_str(value):
    return "" if value is None else str(value).lower()


def _list_lower(values):
    if not values:
        return []
    return [str(v).lower() for v in values]


# ---------------------------------------------------------------- cricket

def test_cricket_worked_example():
    profile = build_other_profile(
        "Cricket Analysis",
        "cricket analysis — matches ki kahani aur stats Urdu mein",
    )
    assert _field(profile, "source") == "other"
    assert _field(profile, "status") == "provisional"
    assert _field(profile, "target_wpm") == 140, (
        f"expected target_wpm=140, got {_field(profile, 'target_wpm')}"
    )
    # language mentions urdu (field may be a string or a list)
    language = _field(profile, "language")
    if isinstance(language, (list, tuple)):
        language_text = " ".join(str(x).lower() for x in language)
    else:
        language_text = _lower_str(language)
    assert "urdu" in language_text, f"language should mention urdu, got {language!r}"

    special_rules = _list_lower(_field(profile, "special_rules"))
    assert special_rules, "special_rules should be non-empty for cricket profile"
    assert any("stat" in rule for rule in special_rules), (
        f"special_rules should contain a stats fact-check rule, got {special_rules!r}"
    )


# ---------------------------------------------------------------- validation

def test_idea_too_short_raises_valueerror():
    with pytest.raises(ValueError):
        build_other_profile("Cricket Analysis", "short")


def test_idea_just_under_limit_raises_valueerror():
    with pytest.raises(ValueError):
        build_other_profile("Valid Name", "123456789")  # 9 chars < 10


def test_name_too_short_raises_valueerror():
    with pytest.raises(ValueError):
        build_other_profile("X", "this idea is long enough to pass validation")


# ---------------------------------------------------------------- health

def test_health_flavoured_idea_is_sensitive():
    profile = build_other_profile(
        "Health Tips",
        "fitness tips and diet plans for diabetes patients",
    )
    assert _field(profile, "risk_class") == "sensitive", (
        f"expected risk_class='sensitive', got {_field(profile, 'risk_class')!r}"
    )
    flags = _list_lower(_field(profile, "sensitive_flags"))
    assert any("health" in flag for flag in flags), (
        f"'health' should be in sensitive_flags, got {flags!r}"
    )
    # custom profiles are still provisional 'other' profiles
    assert _field(profile, "source") == "other"
    assert _field(profile, "status") == "provisional"
