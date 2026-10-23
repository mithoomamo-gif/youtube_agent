"""The 8 built-in Niche Profiles.

Parameters are ported from the working prototype
(``youtube-automation-agent/niches.py``) and the roadmap built-in profile
parameter table (Section 33 — Scripting Workflow):

===================  ============================  =====  ==================
profile              language                      WPM    notes
===================  ============================  =====  ==================
facts                Roman Urdu + English terms    150    fact-log per fact
islamic_stories      Urdu (respectful)             120    sensitive: religion
horror_stories       Roman Urdu                    115    long TTS pauses
history              Roman Urdu                    135    dates/names checked
motivation           Roman Urdu + English quotes   130    stories verifiable
tech                 English + Roman Urdu expl.     145    version/date-stamp
crime_stories        Roman Urdu                    130    sensitive: alleged
health               Roman Urdu + medical terms    135    sensitive: health
===================  ============================  =====  ==================

Hook families reference the roadmap Hook Bank:
H1 Shocking Question, H2 Number + Superlative Promise, H3 Cold-Open Scene,
H4 Mystery Gap, H5 Contrarian Claim, H6 Pain-Point Direct Address,
H7 Date/Prophecy Frame, H8 Before/After Promise.
"""

from __future__ import annotations

from .models import NicheProfile

__all__ = ["BUILTIN_PROFILES", "get_profile"]


_FACTS = NicheProfile(
    profile_id="facts",
    name="Facts / Curiosity",
    source="builtin",
    status="established",
    version=1,
    language="roman_urdu+english",
    tone=["energetic", "curious", "fast-paced"],
    target_wpm=150,
    sentence_style=(
        "Short, punchy spoken sentences; one surprising fact per beat; "
        "English terms kept as-is inside Roman Urdu narration."
    ),
    structure_template=[
        "Hook",
        "Fact chapters (each chapter one new surprise)",
        "Biggest fact (placed near 70% of runtime)",
        "Conclusion + comment question",
    ],
    hook_families=["H1", "H2"],
    voice_style="energetic male/female, clear",
    visual_style="stock footage + bold text overlays + zoom effects",
    cta_style=(
        "Aisi hi hairan kun videos ke liye subscribe karein, aur comment mein "
        "batayein sab se ajeeb fact konsa laga?"
    ),
    risk_class="low",
    sensitive_flags=[],
    taboo=[],
    forbidden_patterns=[
        "fabricated statistics",
        "unverified claim presented as established fact",
        "fabricated quotations",
    ],
    special_rules=[
        "Every fact needs a fact-log entry (all facts are a mandatory fact-check class)",
    ],
)

_ISLAMIC_STORIES = NicheProfile(
    profile_id="islamic_stories",
    name="Islamic Stories",
    source="builtin",
    status="established",
    version=1,
    language="urdu",
    tone=["respectful", "calm", "emotional"],
    target_wpm=120,
    sentence_style=(
        "Slow, clear, reverent Urdu sentences; respectful forms of address; "
        "no casual or slang phrasing around sacred persons or texts."
    ),
    structure_template=[
        "Hook + Bismillah + intro",
        "Background (context of the event)",
        "Main event in detail",
        "Lesson from the event",
        "Dua + conclusion",
    ],
    hook_families=["H3", "H7"],
    voice_style="calm, deep, respectful male voice",
    visual_style=(
        "islamic architecture, nature, soft light, "
        "no faces of Prophets/companions depiction"
    ),
    cta_style=(
        "Allah humein is sabaq par amal ki taufeeq de. "
        "Video pasand aaye to share karein."
    ),
    risk_class="sensitive",
    sensitive_flags=["religion"],
    taboo=[
        "depicting Prophets",
        "depicting the faces of the companions",
        "fabricated narrations",
        "sectarian polemics",
    ],
    forbidden_patterns=[
        "fabricated narrations presented as authentic",
        "paraphrase presented as a direct Quran/Hadith quotation",
        "Quran/Hadith reference without a named source and verified translation",
    ],
    special_rules=[
        "No fabricated narrations",
        "Quran/Hadith references need a named source + verified translation",
        "Religious references are a mandatory fact-check class",
    ],
)

_HORROR_STORIES = NicheProfile(
    profile_id="horror_stories",
    name="Horror Stories",
    source="builtin",
    status="established",
    version=1,
    language="roman_urdu",
    tone=["dark", "suspenseful", "slow-building"],
    target_wpm=115,
    sentence_style=(
        "Slow build-up with short, tense sentences; long pauses marked for "
        "TTS (silence-beat interrupts); present-tense cold-open scenes."
    ),
    structure_template=[
        "Hook scene (warning + cold-open scene)",
        "Character / setting introduction",
        "Escalating strange events",
        "Climax",
        "Open ending + conclusion",
    ],
    hook_families=["H3", "H4"],
    voice_style="low, slow, whisper-like pauses",
    visual_style="dark scenes, fog, old houses, slow zoom",
    cta_style="Kya aap ke saath kabhi aisa hua hai? Comment mein zaroor batayein.",
    risk_class="low",
    sensitive_flags=[],
    taboo=[
        "graphic gore detail",
        "gore instructions",
        "instructions for harmful rituals",
    ],
    forbidden_patterns=[
        "gore instructions",
        "graphic gore description",
        "presenting a fictional story as a verified true event",
    ],
    special_rules=[
        "Long pauses marked for TTS (silence-beat interrupts)",
        "No gore instructions",
    ],
)

_HISTORY = NicheProfile(
    profile_id="history",
    name="History",
    source="builtin",
    status="established",
    version=1,
    language="roman_urdu",
    tone=["documentary", "storytelling", "authoritative"],
    target_wpm=135,
    sentence_style=(
        "Documentary narration sentences; names and dates spoken in full; "
        "chronology always explicit so the timeline is never ambiguous."
    ),
    structure_template=[
        "Hook question (lost secret)",
        "Era background (the world of that time)",
        "Main events in chronological order",
        "Turning point",
        "Legacy / lesson + conclusion",
    ],
    hook_families=["H2", "H4", "H7"],
    voice_style="confident narrator, documentary style",
    visual_style="old maps, monuments, paintings, archive style images",
    cta_style="Aisi tareekhi kahaniyon ke liye channel ko subscribe karein.",
    risk_class="low",
    sensitive_flags=[],
    taboo=[],
    forbidden_patterns=[
        "fabricated dates",
        "fabricated quotations attributed to historical figures",
        "presenting legend or myth as verified historical fact",
    ],
    special_rules=[
        "Dates and names are a mandatory fact-check class",
        "Historical events are a mandatory fact-check class",
    ],
)

_MOTIVATION = NicheProfile(
    profile_id="motivation",
    name="Motivation / Self Improvement",
    source="builtin",
    status="established",
    version=1,
    language="roman_urdu+english",
    tone=["inspiring", "emotional", "direct"],
    target_wpm=130,
    sentence_style=(
        "Direct second-person address with emotional peaks and deliberate "
        "pauses; English quotes kept in English inside Roman Urdu narration."
    ),
    structure_template=[
        "Pain-point hook",
        "Real story / example",
        "3 practical lessons",
        "Action plan",
        "Powerful closing line + conclusion",
    ],
    hook_families=["H6", "H5"],
    voice_style="powerful, emotional, pauses ke saath",
    visual_style="sunrise, gym, city, successful people lifestyle (faceless)",
    cta_style="Aaj se shuru karo. Comment mein 'Main rukunga nahi' likho.",
    risk_class="low",
    sensitive_flags=[],
    taboo=[],
    forbidden_patterns=[
        "fabricated anecdotes presented as true stories",
        "guaranteed success or income promises",
        "fabricated quotations attributed to real people",
    ],
    special_rules=[
        "Stories must be real/verifiable or clearly framed as illustrative",
    ],
)

_TECH = NicheProfile(
    profile_id="tech",
    name="Tech / AI",
    source="builtin",
    status="established",
    version=1,
    language="english+roman_urdu",
    tone=["simple", "smart", "friendly"],
    target_wpm=145,
    sentence_style=(
        "Simple, jargon-light sentences; English product/technical terms "
        "explained in Roman Urdu; one idea per sentence."
    ),
    structure_template=[
        "Hook (bold promise)",
        "What it is — in simple words",
        "How it works",
        "Real uses / benefits",
        "Future + final verdict + conclusion",
    ],
    hook_families=["H1", "H5"],
    voice_style="clear, modern, neutral",
    visual_style="screen recordings, futuristic stock, clean graphics",
    cta_style="Tech ko simple Urdu mein samajhne ke liye subscribe karein.",
    risk_class="low",
    sensitive_flags=[],
    taboo=[],
    forbidden_patterns=[
        "outdated product claims presented as current",
        "fabricated product features, benchmarks or pricing",
        "product claims without a version/date stamp",
    ],
    special_rules=[
        "Version/date-stamp all product claims",
        "Product/version claims are a mandatory fact-check class (fast expiry)",
    ],
)

_CRIME_STORIES = NicheProfile(
    profile_id="crime_stories",
    name="True Crime Stories",
    source="builtin",
    status="established",
    version=1,
    language="roman_urdu",
    tone=["serious", "investigative", "suspenseful"],
    target_wpm=130,
    sentence_style=(
        "Measured, investigative narration; timeline always explicit; "
        "'alleged' / 'suspected' language used until a conviction is stated."
    ),
    structure_template=[
        "Case hook (strangest detail first)",
        "Victim / background",
        "Timeline of events",
        "Investigation + twists",
        "Outcome / court result + conclusion",
    ],
    hook_families=["H3", "H4"],
    voice_style="serious narrator",
    visual_style=(
        "dark city, police tape style graphics, maps, timelines "
        "(sensitive images avoided)"
    ),
    cta_style="Is case par aap ki kya raaye hai? Comment mein batayein.",
    risk_class="sensitive",
    sensitive_flags=["law"],
    taboo=[
        "naming uncharged suspects",
        "glorifying the perpetrator",
        "graphic depiction of violence against victims",
    ],
    forbidden_patterns=[
        "naming uncharged suspects",
        "presenting allegations as proven guilt",
        "glorifying the perpetrator",
    ],
    special_rules=[
        "Use 'alleged'/'suspected' language until conviction",
        "Presumption-of-innocence language is mandatory",
        "No naming uncharged suspects",
    ],
)

_HEALTH = NicheProfile(
    profile_id="health",
    name="Health / Wellness",
    source="builtin",
    status="established",
    version=1,
    language="roman_urdu+medical_terms",
    tone=["caring", "informative", "responsible"],
    target_wpm=135,
    sentence_style=(
        "Calm, clear sentences; English medical terms kept and explained in "
        "simple Roman Urdu; no alarming or absolute phrasing."
    ),
    structure_template=[
        "Myth-bust hook",
        "Science in simple words",
        "Benefits / risks",
        "Practical tips",
        "Disclaimer + conclusion",
    ],
    hook_families=["H5", "H6"],
    voice_style="calm, trustworthy",
    visual_style="healthy food, exercise, human body animations (faceless)",
    cta_style=(
        "Sehat ke liye channel subscribe karein. Kisi bhi ilaaj se pehle "
        "doctor se mashwara karein."
    ),
    risk_class="sensitive",
    sensitive_flags=["health"],
    taboo=[
        "diagnosing individual viewers",
        "prescribing medication or dosage",
        "miracle cure claims",
    ],
    forbidden_patterns=[
        "diagnosis claims",
        "cure claims",
        "dosage instructions",
        "guaranteed treatment outcomes",
    ],
    special_rules=[
        "Disclaimer block is structural and cannot be edited out",
        "No diagnosis or cure claims",
        "Health claims are a mandatory fact-check class",
    ],
)


#: The built-in profile library: exactly the 8 shipped profiles, keyed by
#: ``profile_id``.
BUILTIN_PROFILES: dict[str, NicheProfile] = {
    profile.profile_id: profile
    for profile in (
        _FACTS,
        _ISLAMIC_STORIES,
        _HORROR_STORIES,
        _HISTORY,
        _MOTIVATION,
        _TECH,
        _CRIME_STORIES,
        _HEALTH,
    )
}


def get_profile(profile_id: str) -> NicheProfile:
    """Return the built-in profile for ``profile_id``.

    Raises:
        KeyError: if ``profile_id`` is not one of the 8 built-in profiles.
            ("Other" / custom profiles are built and stored separately by
            the profile auto-build flow; they are not in this dict.)
    """
    return BUILTIN_PROFILES[profile_id]
