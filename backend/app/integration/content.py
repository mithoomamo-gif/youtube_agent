"""Deterministic offline content builder + Gate A for Round-2 integration.

This module is the *offline-deterministic* content stage: given a
:class:`~app.integration.models.ProjectSpec` and a
:class:`~app.niches.models.NicheProfile` it produces a complete
:class:`ContentBundle` (title, hook, chapters, script, TTS-ready
narration, thumbnail text, description, tags, fact-check claim
candidates) with **no network and no LLM** — the same input always
produces the same output (seeded by ``spec.seed`` + a hash of the
topic).

The generation logic is adapted from the working prototype
(``youtube-automation-agent/agent.py`` + ``niches.py``): the prototype's
production-pack ideas — hook-first structure, one surprise per chapter,
the biggest point near the end, conclusion + niche CTA, SEO title /
description / tags and a short thumbnail text — are ported here as
pure Python functions.  Nothing is imported from the prototype path at
runtime.

Round 3 — live Gemini path (:func:`build_content_live`)
-------------------------------------------------------
:func:`build_content` above remains the offline fallback.  The live
entry point is :func:`build_content_live` (sync wrapper:
:func:`build_content_auto`): when a Gemini API key is resolvable
through the injected ``SecretProvider`` (ref ``apikey/gemini`` — env
backend in dev, OS keyring in production; the switch is key
availability, never a code edit) it builds the real
:class:`~app.providers.llm.router.LLMRouter` with only the Gemini
adapter configured (:func:`build_gemini_router`) and generates the
script through the existing agent/prompt layer —
:func:`app.agents.prompt_assembly.assemble_prompt` with the
``outline`` (Agent 6) and ``script_writing`` (Agent 7) production
prompts, outline first, then one call per chapter, then the flagship
final pass (``script.final_pass``).  Tier design (``config/models.yaml``):
outline + chapter writing at **T1** (``gemini-3.8-flash``); the final
pass at **T2**, whose Gemini entry is the Pro-class model
(``gemini-3.1-pro-preview``) — T3 in the registry carries no Gemini
model, so T2 is the flagship slot for a Gemini-only deployment.
Every call goes through the router, so the Budget Guard cap checker
and the append-only LLM call ledger apply unchanged; nothing here
calls a provider directly.  The returned bundle records
``llm_path="gemini-live"`` plus the models / token usage / ledger
cost actually incurred.  With no key resolvable the deterministic
bundle is returned unchanged (``llm_path="deterministic-stub"``).
A *present but invalid* key is NOT silently downgraded: the router's
error (e.g. ``ProviderConfigError`` on HTTP 401) propagates, so a bad
key can never masquerade as a stub run.

Gate A (:func:`run_gate_a`) reuses the existing deterministic services
instead of re-implementing them:

* fact-check — :mod:`app.agents.factcheck`
  (``extract_claim_candidates`` for candidates, ``evaluate_claim`` /
  ``FactLedger`` for verdicts; any RED claim fails the gate),
* retention lint — :mod:`app.agents.retention`
  (``lint_retention`` / ``retention_lint_passed`` over a
  :class:`~app.agents.retention.ScriptSidecar` built from the bundle),
* gate bookkeeping — :func:`app.orchestration.gates.can_pass_gate_a`
  over a synthetic project state built from the two checks above,
* plus a word-count check (narration within ±25% of
  the calibrated word target — the short-test-format tolerance).
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
from typing import Any

from pydantic import BaseModel, Field

from app.agents.factcheck import (
    Claim,
    ClaimType,
    FactLedger,
    Verdict,
    evaluate_claim,
    extract_claim_candidates,
)
from app.agents.retention import (
    ChapterSpan,
    InterruptMarker,
    ScriptSidecar,
    lint_retention,
    retention_lint_passed,
)
from app.integration.planning import plan_word_target
from app.niches.models import NicheProfile
from app.orchestration.gates import can_pass_gate_a

logger = logging.getLogger(__name__)

try:  # Worker 1 owns app.integration.models; code against its contract.
    from app.integration.models import ProjectSpec
except Exception:  # pragma: no cover - fallback until Worker 1 lands
    from pydantic import ConfigDict as _Cfg

    class ProjectSpec(BaseModel):  # type: ignore[no-redef]
        """Local fallback mirroring the shared ProjectSpec contract.

        Used only when ``app.integration.models`` is not importable yet;
        the real model (Worker 1) takes precedence whenever present.
        """

        model_config = _Cfg(extra="ignore")

        niche: str = "facts"
        custom_niche_name: str | None = None
        niche_idea: str | None = None
        topic: str = ""
        minutes: float = 1.0
        language: str | None = None
        seed: int = 0


__all__ = [
    "Chapter",
    "ContentBundle",
    "GEMINI_KEY_REF",
    "LLM_PATH_GEMINI_LIVE",
    "LLM_PATH_STUB",
    "build_content",
    "build_content_auto",
    "build_content_live",
    "build_gemini_router",
    "gemini_key_available",
    "resolve_llm_path",
    "run_gate_a",
]

#: Provenance strings recorded on every ContentBundle (and surfaced by
#: the orchestrator as ``paths_used["llm"]``).
LLM_PATH_STUB = "deterministic-stub"
LLM_PATH_GEMINI_LIVE = "gemini-live"

#: SecretProvider ref for the Gemini API key (see app.providers.secrets_env:
#: resolves from GEMINI_API_KEY in dev, the OS keyring in production).
GEMINI_KEY_REF = "apikey/gemini"

#: Tier design for the live path (config/models.yaml — data, not code):
#: outline (Agent 6) and chapter writing (Agent 7) run at T1; the
#: flagship final pass runs at T2, the tier whose Gemini entry is the
#: Pro-class model (T3 configures no Gemini model).
OUTLINE_TIER = "T1"
CHAPTER_TIER = "T1"
FINAL_PASS_TIER = "T2"


# ---------------------------------------------------------------------------
# Bundle schema (shared contract)
# ---------------------------------------------------------------------------


class Chapter(BaseModel):
    """One script chapter: 1-based ``index``, display ``title``, spoken ``text``."""

    index: int
    title: str
    text: str


class ContentBundle(BaseModel):
    """Everything the downstream Round-2 stages need from the content stage."""

    title: str
    hook: str
    chapters: list[Chapter]
    script_text: str  # full narration, chapters joined (with chapter headings)
    narration_text: str  # clean text for TTS: no markdown / stage directions
    thumbnail_text: str  # short, 2-4 words
    description: str
    tags: list[str]
    claims: list[dict]  # fact-check candidates: {text, type, chapter}
    # Bookkeeping for Gate A / downstream stages (not part of the pack UI).
    target_words: int = 0
    target_minutes: float = 0.0
    climax_chapter: int = 0  # 1-based chapter index carrying the climax
    # Provenance (Round 3): which LLM path produced this bundle —
    # "deterministic-stub" (offline, $0) or "gemini-live" (real router).
    llm_path: str = LLM_PATH_STUB
    #: Physical models actually used, in first-use order (live path only).
    llm_models: list[str] = Field(default_factory=list)
    #: Aggregated token usage {"input_tokens", "output_tokens", "calls"}.
    llm_usage: dict = Field(default_factory=dict)
    #: Total ledger cost in USD of the live calls (0.0 for the stub).
    llm_cost_usd: float = 0.0
    # Planning provenance (Round 4): the WPM the script-length plan
    # actually used — the selected voice's MEASURED WPM from the
    # versioned calibration store when available (Round 3 measured
    # Urdu edge-tts at ~199-225 WPM vs the 110-160 planning band), else
    # the profile's planning constant.  ``wpm_provenance`` carries the
    # full audit record ({"source": "measured", version, measured_at,
    # method, path} or {"source": "unmeasured-default", ...}); it is
    # NEVER absent on bundles built by this module's builders.
    planning_wpm: float = 0.0
    planning_voice_id: str = ""
    planning_rate_pct: float = 0.0
    wpm_provenance: dict = Field(default_factory=dict)
    # Style Lab (style_profile.v1): the style params actually applied
    # from the channel's resolved style profile, shaped
    # ``{"script": {...}, "seo": {...}}``; ``None`` when no profile
    # resolved (behaviour is then byte-identical to pre-Style-Lab).
    style_applied: dict | None = None


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "ke", "ki", "ka", "aur",
    "mein", "hai", "hain", "ye", "yeh", "woh", "wo", "kya", "kyun",
    "and", "or", "is", "are", "was", "were", "with", "for", "par",
}


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


def _word_count(text: str) -> int:
    return len(_words(text))


def _seeded_rng(spec: Any, attempt: int = 0) -> random.Random:
    """RNG seeded by ``spec.seed`` + topic hash (stable across runs).

    ``attempt`` salts the stream for build_content's deterministic
    re-rolls (see there); attempt 0 is the canonical stream.
    """
    topic = str(getattr(spec, "topic", "") or "")
    niche = str(getattr(spec, "niche", "") or "")
    seed = getattr(spec, "seed", 0)
    material = f"{seed}|{niche}|{topic}"
    if attempt:
        material += f"|{attempt}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _keywords(topic: str, limit: int = 4) -> list[str]:
    """Topic keywords (stopwords removed), order preserved, deduped."""
    out: list[str] = []
    for word in _words(topic):
        low = word.lower()
        if low in _STOPWORDS or len(word) < 3:
            continue
        if low not in [k.lower() for k in out]:
            out.append(word)
        if len(out) >= limit:
            break
    return out


def _language_mode(spec: Any, profile: NicheProfile) -> str:
    """Resolve the narration flavour: 'english' or 'roman_urdu' (default mix).

    Note the shared ``ProjectSpec.language`` is a short code (``"ur"``
    by default, ``"en"`` for English) while ``NicheProfile.language``
    is a mix label (``"roman_urdu+english"`` ...).  An explicit English
    spec code wins; a pure-English profile wins next; everything else
    narrates in the Roman-Urdu mix (English terms kept natural),
    matching the prototype's profiles.
    """
    spec_lang = str(getattr(spec, "language", None) or "").strip().lower()
    if spec_lang in ("en", "english", "en-us", "en-gb"):
        return "english"
    if str(profile.language or "").strip().lower() == "english":
        return "english"
    return "roman_urdu"


# ---------------------------------------------------------------------------
# Sentence banks (ported from the prototype's per-niche structure ideas:
# hook = shocking question / surprising fact, one new point per chapter,
# biggest point near 70%, conclusion + CTA).  ``{topic}`` / ``{kw}`` /
# ``{niche}`` are filled deterministically.  No greetings anywhere — the
# hook must open with proof (retention rule 7).
# ---------------------------------------------------------------------------

_HOOK_RU = [
    "{topic} — is ke baare mein ek baat aisi hai jo zyada tar log bilkul nahi jaante, aur wahi baat sab kuch badal deti hai.",
    "{topic} ka sab se hairan kun pehlu pehle hi minute mein saamne aa jata hai, bas aksar log usay notice hi nahi karte.",
    "Ek sawal se shuru karte hain: {topic} ke peeche asal kahani kya hai? Jawab aap ki soch se zyada ajeeb hai.",
    "{topic} — sunne mein simple lagta hai, lekin is ki tafseel mein ek aisa mod hai jo poori tasveer palat deta hai.",
]
_HOOK_EN = [
    "{topic} — there is one detail about this that most people never notice, and it changes the whole picture.",
    "Start with the strangest part: {topic} hides a twist that almost every short summary leaves out.",
    "One question proves the point fast: what is really going on with {topic}? The answer is stranger than expected.",
]

_BODY_RU = [
    "Pehle ye pehlu simple alfaaz mein samjhein: {topic} seedha roz-marra misaalon se judta hai.",
    "Ek ahem nuqta aksar nazar-andaz ho jata hai: {topic} ki chhoti tafseelat bari tasveer banati hain.",
    "Is marhale par kahani naya mod leti hai, kyun ke {kw} ki har baat pichli ko wazeh karti hai.",
    "Ghaur karein: {topic} ki sab se aam cheez hi sab se zyada sawal uthati hai.",
    "Experts jab bhi {kw} ka zikr karte hain, sab se pehle isi pehlu ko dekhte hain.",
    "Ye point yaad rakhein: {topic} ka sab se bara raaz aage chal kar isi se khulega.",
    "{topic} ko samjhe baghair poori kahani adhoori rehti hai, is liye ye hissa ahem hai.",
    "Aksar log sirf surface dekhte hain, jab ke {topic} ki gehraai peeche chhupi hoti hai.",
]
_BODY_EN = [
    "Put this part in simple words: {topic} connects directly to everyday examples.",
    "One point is usually missed: the small details of {topic} form one bigger picture.",
    "The story turns here, because every fact about {kw} makes the previous one clearer.",
    "Notice this: the most ordinary part of {topic} raises the biggest questions.",
    "Whenever {kw} comes up, experts check this angle first, for a simple reason.",
    "Remember this point: the biggest reveal about {topic} opens from exactly here.",
]

_INTRO_RU = [
    "Ab pas-manzar samajh lein: {topic} ki bunyaad hi isay itna dilchasp banati hai.",
    "Shuruaat se chalna hoga, kyun ke {topic} ka har aham mod yahi se nikalta hai.",
]
_INTRO_EN = [
    "Now the background, briefly: the foundation of {topic} is what makes it interesting.",
]

# Open loops appended to every non-final chapter (retention rule 5).
_OPEN_LOOP_RU = [
    "Lekin asal sawal abhi baaki hai.",
    "Aur yahi se kahani aur ajeeb hoti jati hai.",
    "Magar ek baat abhi tak samajh nahi aayi.",
    "Lekin rukye, sab se ahem hissa abhi aana baaki hai.",
]
_OPEN_LOOP_EN = [
    "But the real question is still open.",
    "And this is where the story gets stranger.",
    "But one thing still does not add up.",
]

_CLIMAX_RU = [
    "Ab wo lamha aa gaya: {topic} ka sab se bara raaz hamesha saamne tha, bas angle badalna tha.",
    "Yahi turning point hai: {topic} ki ek baat poori tasveer badal deti hai.",
    "Sab se ahem inkishaaf: {kw} ki ye tafseel {topic} ki samajh badal deti hai.",
]
_CLIMAX_EN = [
    "Now the reveal: the biggest secret of {topic} was in plain sight all along.",
    "The turning point: one detail about {kw} changes everything about {topic}.",
]

_FILLER_RU = [
    "Yehi wajah hai ke {topic} par baat karte waqt log aksar hairan reh jate hain.",
    "Isi liye is topic ko samajhna sirf dilchasp nahi, waqai ahem bhi hai.",
    "Har nayi tafseel ke saath {kw} ki ahmiyat aur zyada wazeh hoti jati hai.",
    "Aur yehi chhoti si baat aage ja kar sab se bara farq paida karti hai.",
    "Dekha jaye to {topic} ki khoobsurati hi is ki inhi tafseelat mein hai.",
    "Is point par ruk kar sochna banta hai, kyun ke yehi asal sabaq chhupa hai.",
]
_FILLER_EN = [
    "That is exactly why people are usually surprised when {topic} comes up.",
    "And that is why understanding this topic is not just interesting, it genuinely matters.",
    "With every new detail, the importance of {kw} becomes clearer and clearer.",
    "And this small point is what later makes the biggest difference of all.",
]

# Short-form variants (used when the target is < 140 words, i.e. under
# ~1 minute): the standard banks above are built from 13-26 word
# sentences, whose *minimum* chapter compositions (hook 29 + climax 21
# + conclusion 28 words) exceed a 75-word target outright and pin the
# climax near 55% no matter how the balancer shuffles middle sentences.
# These concise variants keep the same structure and rules (proof-first
# hook, one point per chapter, late climax, CTA ending) at 6-13 words
# per sentence, so a short script can legitimately satisfy Gate A.
_HOOK_RU_SHORT = [
    "{topic}: ek baat aisi hai jo sab kuch badal deti hai.",
    "{topic} ka sab se bara raaz aaj khulne wala hai.",
    "Sawal seedha sa hai: {topic} asal mein hai kya?",
]
_HOOK_EN_SHORT = [
    "{topic}: one small detail about this changes the whole picture.",
    "The strangest part of {topic} is simpler than you think.",
]
_INTRO_RU_SHORT = [
    "Pas-manzar: {topic} ki bunyaad hi kamaal hai.",
    "Shuruaat yahi se hoti hai: {topic} ke saath.",
]
_INTRO_EN_SHORT = [
    "Background first: the base of {topic} is the real key.",
]
_BODY_RU_SHORT = [
    "{topic} roz-marra zindagi se seedha judta hai.",
    "Chhoti tafseelat hi {kw} ki bari tasveer banati hain.",
    "Experts {kw} mein sab se pehle yahi pehlu dekhte hain.",
    "{topic} ki gehraai aksar nazron se chhup jati hai.",
]
_BODY_EN_SHORT = [
    "{topic} connects directly to everyday examples around us.",
    "The small details of {kw} form the one bigger picture.",
    "Experts check this angle of {kw} before anything else.",
]
_CLIMAX_RU_SHORT = [
    "Sab se bara inkishaaf: {kw} hi asal mod hai.",
    "Yahi turning point hai: {topic} ki tasveer badal gayi.",
]
_CLIMAX_EN_SHORT = [
    "The reveal: {kw} changes everything about {topic} completely.",
]
_FILLER_RU_SHORT = [
    "Yehi baat {kw} ko khaas banati hai.",
    "Har tafseel ke saath tasveer saaf hoti hai.",
    "Aur yehi point aage sab se ahem hoga.",
    "Yehi baat sab kuch hai.",
]
_FILLER_EN_SHORT = [
    "This is what makes {kw} truly special.",
    "Every new detail makes the picture clearer.",
    "And that changes everything here.",
]
# Short-form CTA: keeps the profile CTA's two jobs (subscribe +
# comment question) in one tight line, used only when the profile's
# full CTA is too long for a sub-1-minute script.  Kept to ~9 words:
# at a 60-word target the conclusion's fixed mass decides whether
# Gate A's 65% climax floor is reachable at all.
_SHORT_CTA_RU = "Subscribe karein, aur comment mein apni raye zaroor dein."
_SHORT_CTA_EN = "Subscribe for more, and share your take in comments."


# ---------------------------------------------------------------------------
# Structure handling
# ---------------------------------------------------------------------------


def _section_role(section: str, position: int, total: int) -> str:
    """Classify a structure-template section: hook/intro/main/climax/conclusion."""
    low = section.lower()
    if position == total - 1 or "conclusion" in low or "cta" in low:
        return "conclusion"
    if position == 0 or "hook" in low:
        return "hook"
    if any(k in low for k in ("climax", "biggest", "sab se bara", "turning point", "key insight", "myth-bust")):
        return "climax"
    if any(k in low for k in ("intro", "background", "pas-manzar", "context", "setting", "what it is", "pas manzar")):
        return "intro"
    return "main"


def _plan_sections(profile: NicheProfile, target_words: int = 0) -> list[tuple[str, str]]:
    """Return ``[(title, role)]`` with 3-5 entries, hook first, conclusion last.

    Works for ANY profile: rich built-in templates are used as-is
    (merged down to 5 when longer); a minimal template falls back to a
    data-only generic structure (hook / context / main / climax /
    conclusion) derived from the profile's own name.  Very short
    targets (< 130 words) cap at 4 chapters: five mandatory
    primary+closing blocks cannot fit a ~2-minute script otherwise.
    """
    raw = [str(s).strip() for s in (profile.structure_template or []) if str(s).strip()]
    if len(raw) < 3:
        raw = [
            "Hook",
            f"{profile.name} — context",
            "Main chapters",
            "Climax — key insight",
            "Conclusion + CTA",
        ]
    if len(raw) > 5:
        # Keep hook + conclusion; collapse the middle into 3 chapters
        # (extra middle sections are merged, never dropped silently —
        # their content role is covered by the generic middle chapter).
        middle = raw[1:-1]
        raw = [raw[0], middle[0], "Main chapters (continued)", middle[-1], raw[-1]]
    if 0 < target_words < 130 and len(raw) > 4:
        # Short format: keep hook, one middle chapter, the climax
        # section (or the last middle) and the conclusion.
        climax_idx = next(
            (
                i
                for i, s in enumerate(raw)
                if any(k in s.lower() for k in ("climax", "biggest", "turning"))
            ),
            len(raw) - 2,
        )
        keep = [0, 1, climax_idx, len(raw) - 1]
        raw = [raw[i] for i in dict.fromkeys(keep)]
    roles = [_section_role(s, i, len(raw)) for i, s in enumerate(raw)]
    # Guarantee exactly one climax, placed just before the conclusion
    # when the template does not name one.
    if "climax" not in roles:
        roles[-2] = "climax"
    else:
        first = roles.index("climax")
        roles = [r if (i == first or r != "climax") else "main" for i, r in enumerate(roles)]
    roles[0] = "hook"
    roles[-1] = "conclusion"
    return list(zip(raw, roles))


def _budgets(roles: list[str], target_words: int) -> list[int]:
    """Per-chapter word budgets: climax starts at ~65% of the narration.

    Conclusion takes ~15%, the climax chapter ~20%, and everything
    before the climax shares the remaining ~65% (hook chapter gets a
    smaller slice of that share so the opening stays tight).
    """
    n = len(roles)
    climax_idx = roles.index("climax")
    conclusion_share = 0.13
    climax_share = 0.17
    pre_share = 1.0 - conclusion_share - climax_share  # 0.70 pre-climax
    budgets = [0] * n
    budgets[-1] = max(8, round(target_words * conclusion_share))
    budgets[climax_idx] = max(8, round(target_words * climax_share))
    pre = [i for i in range(n) if i not in (climax_idx, n - 1)]
    if pre:
        # Hook chapter: 30% of the pre-climax share; the rest split evenly.
        hook_share = pre_share * 0.30 if len(pre) > 1 else pre_share
        rest = (pre_share - hook_share) / (len(pre) - 1) if len(pre) > 1 else 0.0
        for j, i in enumerate(pre):
            share = hook_share if j == 0 else rest
            budgets[i] = max(6, round(target_words * share))
    # Fix rounding drift on the largest pre-climax chapter so the total
    # lands on target (build tolerance is ±20%; this keeps us ~exact).
    drift = target_words - sum(budgets)
    if pre and drift:
        biggest = max(pre, key=lambda i: budgets[i])
        budgets[biggest] = max(6, budgets[biggest] + drift)
    return budgets


# ---------------------------------------------------------------------------
# Style Lab steering (style_profile.v1)
#
# When the spec resolves a channel style profile (see
# app.integration.style_profile), the profile's PATTERNS steer this
# stage: chapter count / word budgets / hook window for the script and
# the title formula + description structure for SEO.  Patterns only —
# a profile contains no competitor text or footage by design (the
# no-copy rule), the style-instruction block tells the live model to
# follow the FORMAT with original content, and nothing here bypasses
# Gate A: the bundle still goes through the same word-target /
# retention / fact-check machinery, with the style target recorded on
# the bundle like any other plan ("profile informs, gates decide").
# ---------------------------------------------------------------------------


def _resolve_style(spec: Any, base_dir: Any = None) -> tuple[Any, dict] | None:
    """``(profile, directives)`` for ``spec``, or None when unstyled.

    Resolution order lives in
    :func:`app.integration.style_profile.resolve_style_profile`:
    ``spec.style_profile_path`` -> ``<base_dir>/style_profiles/
    <channel_id>.json`` -> env ``YTA_STYLE_PROFILE_PATH``.  An
    explicitly configured but missing profile FAILS LOUD there
    (R4-003) and the error propagates — style is never silently
    dropped or substituted.  The content builders receive no project
    base dir in their contract, so the channel-default step resolves
    against the working directory (the produce run's base dir in
    practice); the spec-path and env steps are exact regardless.
    """
    from pathlib import Path

    from app.integration.style_profile import (
        resolve_style_profile,
        style_directives,
    )

    base = Path(base_dir) if base_dir else Path.cwd()
    profile = resolve_style_profile(spec, base)
    if profile is None:
        return None
    directives = style_directives(profile)
    return profile, directives if isinstance(directives, dict) else {}


def _style_float(directives: dict, key: str) -> float | None:
    """A positive float directive, or None ("no opinion" / unusable)."""
    value = directives.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _style_target_words(directives: dict) -> int | None:
    """Overall word target: ``speech_wpm x target_duration_s``."""
    wpm = _style_float(directives, "speech_wpm")
    duration = _style_float(directives, "target_duration_s")
    if wpm is None or duration is None:
        return None
    return max(30, int(round(wpm * duration / 60.0)))


def _style_plan(
    profile: NicheProfile, directives: dict, target_words: int
) -> list[tuple[str, str]]:
    """Chapter plan steered to ``chapters_target`` (+-1 tolerance).

    Starts from the niche template plan (:func:`_plan_sections`) and
    expands it with generic main chapters before the climax, or
    contracts it by dropping middle main/intro sections — the hook,
    climax and conclusion are never dropped.  The count is clamped to
    3..10 so a bad profile cannot produce an absurd plan; Gate A's
    structural checks still judge the result.
    """
    plan = list(_plan_sections(profile, target_words))
    raw = directives.get("chapters_target")
    try:
        desired = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        desired = 0
    if desired <= 0:
        return plan
    desired = max(3, min(10, desired))
    if len(plan) < desired:
        climax_idx = [role for _, role in plan].index("climax")
        part = 2
        while len(plan) < desired:
            plan.insert(climax_idx, (f"Main chapters — part {part}", "main"))
            climax_idx += 1
            part += 1
    elif len(plan) > desired:
        while len(plan) > desired:
            removable = [
                i for i, (_, role) in enumerate(plan)
                if role in ("main", "intro")
            ]
            if not removable:
                break
            plan.pop(removable[len(removable) // 2])
    return plan


def _style_budgets(
    roles: list[str], target_words: int, directives: dict
) -> list[int]:
    """Per-chapter word budgets from ``speech_wpm x avg_chapter_s``.

    The hook chapter's budget derives from ``speech_wpm x
    hook_window_s`` (the hook must land inside its window); every
    other chapter starts from ``speech_wpm x avg_chapter_s``.
    Rounding drift against the overall word target is distributed
    over the non-hook chapters so the hook budget stays exact.
    Without a usable speech WPM the standard share-based budgets
    apply unchanged.
    """
    wpm = _style_float(directives, "speech_wpm")
    if wpm is None:
        return _budgets(roles, target_words)
    avg_s = _style_float(directives, "avg_chapter_s")
    hook_s = _style_float(directives, "hook_window_s")
    avg_words = (
        wpm * avg_s / 60.0 if avg_s else target_words / max(len(roles), 1)
    )
    hook_words = wpm * hook_s / 60.0 if hook_s else avg_words
    budgets = [
        max(6, int(round(hook_words if role == "hook" else avg_words)))
        for role in roles
    ]
    drift = target_words - sum(budgets)
    if drift:
        others = [i for i, role in enumerate(roles) if role != "hook"]
        if not others:
            others = list(range(len(roles)))
        share, remainder = divmod(drift, len(others))
        for j, i in enumerate(others):
            budgets[i] = max(
                6, budgets[i] + share + (1 if j < remainder else 0))
    return budgets


def _script_style_applied(
    directives: dict,
    plan: list[tuple[str, str]],
    budgets: list[int],
    target_words: int,
) -> dict:
    """The ``style_applied["script"]`` record: params actually used."""
    wpm = _style_float(directives, "speech_wpm")
    avg_s = _style_float(directives, "avg_chapter_s")
    hook_s = _style_float(directives, "hook_window_s")
    return {
        "target_duration_s": _style_float(directives, "target_duration_s"),
        "speech_wpm": wpm,
        "target_words": target_words,
        "chapters_target": directives.get("chapters_target"),
        "chapters_planned": len(plan),
        "hook_window_s": hook_s,
        "hook_types": list(directives.get("hook_types") or []),
        "hook_budget_words": (
            int(round(wpm * hook_s / 60.0)) if wpm and hook_s else None
        ),
        "avg_chapter_s": avg_s,
        "avg_chapter_words": (
            int(round(wpm * avg_s / 60.0)) if wpm and avg_s else None
        ),
        "chapter_word_budgets": list(budgets),
    }


def _style_instruction_block(
    directives: dict,
    plan: list[tuple[str, str]],
    budgets: list[int],
    target_words: int,
) -> str:
    """Style-instruction block appended to live outline/chapter prompts.

    Structure / pacing / hook FORMAT only.  A style profile contains
    no competitor text by design, so nothing here can inject any; the
    block additionally instructs the model, explicitly, that all
    content must be original.
    """
    wpm = _style_float(directives, "speech_wpm")
    duration = _style_float(directives, "target_duration_s")
    hook_s = _style_float(directives, "hook_window_s")
    avg_s = _style_float(directives, "avg_chapter_s")
    hook_types = [str(t) for t in (directives.get("hook_types") or []) if t]
    lines = ["STYLE PROFILE — follow this FORMAT, structure and pacing:"]
    if duration and wpm:
        lines.append(
            f"- Total narration: ~{target_words} words for a "
            f"~{duration:g}-second video at ~{wpm:g} words per minute.")
    else:
        lines.append(f"- Total narration: ~{target_words} words.")
    hook_line = (
        f"- Chapters: exactly {len(plan)}; chapter 1 is the hook"
    )
    if hook_s:
        hook_line += (
            f" and must land its hook within the first {hook_s:g} "
            f"seconds (~{budgets[0] if budgets else 0} words)")
    lines.append(hook_line + ".")
    if hook_types:
        lines.append(f"- Hook type to use: {', '.join(hook_types)}.")
    if avg_s:
        lines.append(
            f"- Average chapter length ~{avg_s:g} seconds; per-chapter "
            f"word budgets: {', '.join(str(b) for b in budgets)}.")
    lines.append(
        "- The title and description are generated separately to a "
        "fixed formula; write narration only.")
    lines.append(
        "ORIGINALITY (mandatory): every word, example and phrasing "
        "must be your own original work written for THIS topic. Never "
        "copy or closely paraphrase any other creator's script, "
        "title, description or wording.")
    return "\n".join(lines)


def _style_title(
    topic: str,
    directives: dict,
    rng: random.Random,
    ru: bool,
    chapter_count: int,
) -> tuple[str, dict]:
    """A title conforming to the profile's ``title_formula``.

    Formula conformance on OUR OWN generated text: length inside
    ``[len_min, len_max]``; a number when ``number_rate >= 0.5`` (the
    count of main points this video actually covers); the dominant
    separator — brackets when ``bracket_rate >= 0.5``, otherwise a
    pipe; an ALL-CAPS suffix when ``caps_rate >= 0.5``; and the topic
    phrase at the profile's ``keyword_position`` (start/middle/end).
    """
    formula = directives.get("title_formula") or {}

    def _rate(key: str) -> float:
        value = formula.get(key)
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _band(key: str) -> int:
        value = formula.get(key)
        try:
            return int(value) if value else 0
        except (TypeError, ValueError):
            return 0

    len_min = _band("len_min")
    len_max = _band("len_max")
    number_included = _rate("number_rate") >= 0.5
    caps_applied = _rate("caps_rate") >= 0.5
    separator = "bracket" if _rate("bracket_rate") >= 0.5 else "pipe"
    position = str(formula.get("keyword_position") or "start").lower()
    if position not in ("start", "middle", "end"):
        position = "start"

    suffixes = _TITLE_SUFFIX_RU if ru else _TITLE_SUFFIX_EN
    suffix = suffixes[rng.randrange(len(suffixes))]
    if number_included:
        points = max(1, chapter_count - 2)
        suffix = f"{points} Ahem Baatein" if ru else f"{points} Key Facts"
    if caps_applied:
        suffix = suffix.upper()
    lead = "Sach Ye Hai" if ru else "The Real Story"

    def build(suffix_text: str) -> str:
        if position == "end":
            return (
                f"{suffix_text} [{topic}]"
                if separator == "bracket"
                else f"{suffix_text} | {topic}"
            )
        if position == "middle":
            return (
                f"{lead}: {topic} [{suffix_text}]"
                if separator == "bracket"
                else f"{lead} | {topic} | {suffix_text}"
            )
        return (
            f"{topic} [{suffix_text}]"
            if separator == "bracket"
            else f"{topic} | {suffix_text}"
        )

    title = build(suffix)
    if len_max and len(title) > len_max:
        # Shrink the suffix first — the topic phrase is never cut
        # until it is the only thing left.
        words = suffix.split()
        while words and len(title) > len_max:
            words.pop()
            title = build(" ".join(words)) if words else topic
        if len(title) > len_max:
            title = title[:len_max].rstrip()
    if len_min and len(title) < len_min:
        tails = (
            ["Poori Kahani", "Asal Sach"]
            if ru
            else ["The Full Story", "The Untold Truth"]
        )
        for tail in tails:
            if len(title) >= len_min:
                break
            title = (
                f"{title} [{tail}]"
                if separator == "bracket"
                else f"{title} | {tail}"
            )
        if len_max and len(title) > len_max:
            # Padding appends WHOLE tails, so it can overshoot the
            # band (proof finding, Style Lab: band [60, 60] produced a
            # 71-char title). The band's upper edge always wins —
            # trim exactly as the shrink path above does.
            title = title[:len_max].rstrip()
    applied = {
        "len_min": len_min or None,
        "len_max": len_max or None,
        "length": len(title),
        "number_included": number_included,
        "separator": separator,
        "caps_applied": caps_applied,
        "keyword_position": position,
    }
    return title, applied


def _style_description(
    topic: str,
    niche_label: str,
    chapters: list[Chapter],
    profile: NicheProfile,
    directives: dict,
    ru: bool,
    tags: list[str],
    channel_id: Any = None,
) -> tuple[str, dict]:
    """A description following the profile's ``description_structure``.

    Sections render in the profile's order (intro / chapters / cta /
    hashtags / links, plus any unrecognised label as its own headed
    section); links render only when ``link_expected``; the hashtag
    count — built from OUR OWN tags — is clamped into
    ``[hashtag_min, hashtag_max]`` and appended at the end when the
    section list omitted it but the band requires hashtags.
    """
    structure = directives.get("description_structure") or {}
    sections = [
        str(s).strip().lower()
        for s in (structure.get("sections") or [])
        if str(s).strip()
    ]

    def _count(key: str) -> int:
        value = structure.get(key)
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    hashtag_min = _count("hashtag_min")
    hashtag_max = _count("hashtag_max")
    link_expected = bool(structure.get("link_expected"))

    if ru:
        detail_line = (
            f"Is video mein aap jaanenge {topic} ki wo tafseelat jo "
            "zyada tar log nahi jaante."
        )
        intro_block = f"{topic} — {niche_label} par mukammal video.\n\n{detail_line}"
        links_block = (
            "Mazeed videos ke liye hamara channel dekhein: "
            f"{channel_id or 'local-test-channel'}"
        )
    else:
        detail_line = (
            f"In this video you will learn the details of {topic} "
            "that most people never hear."
        )
        intro_block = f"{topic} — a complete video on {niche_label}.\n\n{detail_line}"
        links_block = (
            f"More videos on this channel: {channel_id or 'local-test-channel'}"
        )
    chapter_lines = "\n".join(
        f"- Chapter {c.index}: {c.title}" for c in chapters)
    chapters_block = f"Chapters:\n{chapter_lines}"
    cta_block = str(profile.cta_style).strip()

    hashtag_pool: list[str] = []
    for tag in tags:
        cleaned = re.sub(r"[^\w]", "", str(tag).replace(" ", ""), flags=re.UNICODE)
        if cleaned and cleaned.lower() not in [h.lower() for h in hashtag_pool]:
            hashtag_pool.append(cleaned)
    n_tags = len(hashtag_pool)
    if hashtag_max > 0:
        n_tags = min(n_tags, hashtag_max)
    if n_tags < hashtag_min:
        extras = _keywords(topic) + [niche_label]
        for extra in extras:
            if n_tags >= hashtag_min:
                break
            cleaned = re.sub(
                r"[^\w]", "", str(extra).replace(" ", ""), flags=re.UNICODE)
            if cleaned and cleaned.lower() not in [h.lower() for h in hashtag_pool]:
                hashtag_pool.append(cleaned)
                n_tags += 1
    hashtag_line = " ".join(f"#{tag}" for tag in hashtag_pool[:n_tags])

    def _kind(token: str) -> str:
        if "hash" in token or token == "tags":
            return "hashtags"
        if "chapter" in token or "timestamp" in token:
            return "chapters"
        if "cta" in token or "subscri" in token:
            return "cta"
        if "link" in token or "social" in token:
            return "links"
        if "intro" in token or "hook" in token or "about" in token \
                or "desc" in token:
            return "intro"
        return "generic"

    blocks = {
        "intro": intro_block,
        "chapters": chapters_block,
        "cta": cta_block,
        "links": links_block,
        "hashtags": hashtag_line,
    }
    order = sections or (
        ["intro", "chapters", "cta"]
        + (["links"] if link_expected else [])
        + (["hashtags"] if (hashtag_min > 0 or hashtag_max > 0) else [])
    )
    parts: list[str] = []
    rendered: list[str] = []
    seen: set[str] = set()
    for token in order:
        kind = _kind(token)
        if kind in seen:
            continue
        if kind == "links" and not link_expected:
            continue
        if kind == "hashtags" and not hashtag_line:
            continue
        if kind == "generic":
            block = f"{token.title()}:\n{detail_line}"
        else:
            block = blocks[kind]
        if not block:
            continue
        seen.add(kind)
        rendered.append(kind if kind != "generic" else token)
        parts.append(block)
    # Band/link enforcement even when the section list omitted them.
    if hashtag_min > 0 and "hashtags" not in seen and hashtag_line:
        seen.add("hashtags")
        rendered.append("hashtags")
        parts.append(hashtag_line)
    if link_expected and "links" not in seen:
        seen.add("links")
        rendered.append("links")
        parts.append(links_block)
    description = "\n\n".join(parts)
    applied = {
        "sections": rendered,
        "hashtag_count": len(re.findall(r"#\w+", description)),
        "hashtag_min": hashtag_min or None,
        "hashtag_max": hashtag_max or None,
        "link_included": "links" in seen,
    }
    return description, applied


def _sized_filler(
    bank: list[str], fmt: dict[str, str], start_idx: int, max_words: int
) -> str:
    """First rotated filler fitting ``max_words`` (else the shortest one).

    Prevents a long filler sentence from blowing a small chapter's
    budget when only a few words are still needed.
    """
    rotated = [bank[(start_idx + k) % len(bank)].format(**fmt) for k in range(len(bank))]
    for sentence in rotated:
        if _word_count(sentence) <= max_words:
            return sentence
    return min(rotated, key=_word_count)


def _balance_chapters(
    chapter_sents: list[list[str]],
    budgets: list[int],
    climax_idx: int,
    target_words: int,
    filler_bank: list[str],
    fmt: dict[str, str],
    rng: random.Random,
) -> None:
    """Adjust sentence lists in place so the bundle hits its targets.

    Guarantees (best effort, deterministic): total words within ~±12%
    of ``target_words`` and the climax chapter starting at 66-74% of
    the narration.  The first sentence of each chapter and its closing
    sentence (open loop / CTA) are protected; only middle sentences are
    ever dropped, and filler is always inserted *before* the closing.
    """
    if not chapter_sents or not filler_bank:
        return

    def words_of(sents: list[str]) -> int:
        return sum(_word_count(s) for s in sents)

    def total() -> int:
        return sum(words_of(s) for s in chapter_sents)

    def climax_start() -> int:
        return sum(words_of(s) for s in chapter_sents[:climax_idx])

    state = {"idx": rng.randrange(len(filler_bank))}

    def add_filler(ci: int, max_words: int | None = None) -> None:
        if max_words is None:
            sentence = filler_bank[state["idx"] % len(filler_bank)].format(**fmt)
        else:
            sentence = _sized_filler(filler_bank, fmt, state["idx"], max_words)
        state["idx"] += 1
        chapter_sents[ci].insert(len(chapter_sents[ci]) - 1, sentence)

    def removable(ci: int) -> list[int]:
        return list(range(1, max(1, len(chapter_sents[ci]) - 1)))

    def drop_longest(ci: int) -> bool:
        idxs = removable(ci)
        if not idxs:
            return False
        j = max(idxs, key=lambda k: _word_count(chapter_sents[ci][k]))
        chapter_sents[ci].pop(j)
        return True

    # 1) Climax position into the 66-74% band.  When the total is
    # already near the cap, raise the ratio by dropping removable
    # sentences from the climax / post-climax chapters instead of
    # growing the pre-climax chapters.
    guard = 0
    while total() > 0 and climax_start() / total() < 0.66 and guard < 800:
        guard += 1
        post = [ci for ci in range(climax_idx, len(chapter_sents)) if removable(ci)]
        if post and total() > target_words * 1.05:
            drop_longest(max(post, key=lambda c: words_of(chapter_sents[c])))
        elif total() < target_words * 1.20:
            pre = list(range(climax_idx)) or [0]
            # Size the filler to the room left under Gate A's word
            # cap (+25%): an uncapped 7-9 word filler can push a
            # near-cap total 1-2 words over the limit while chasing
            # the climax ratio.
            room = int(target_words * 1.25) - total() + 1
            add_filler(
                max(pre, key=lambda c: words_of(chapter_sents[c])),
                max_words=max(4, room),
            )
        elif post:
            drop_longest(max(post, key=lambda c: words_of(chapter_sents[c])))
        else:
            break
    guard = 0
    while total() > 0 and climax_start() / total() > 0.74 and guard < 500:
        guard += 1
        pre = [ci for ci in range(climax_idx) if removable(ci)]
        if not pre:
            break
        if not drop_longest(max(pre, key=lambda c: words_of(chapter_sents[c]))):
            break

    # 2) Trim overshoot from the chapters furthest over budget
    # (post-climax first, so trimming cannot wreck the climax ratio).
    guard = 0
    while total() > target_words * 1.15 and guard < 500:
        guard += 1
        cands = [ci for ci in range(len(chapter_sents)) if removable(ci)]
        if not cands:
            break
        post = [ci for ci in cands if ci >= climax_idx]
        pool_c = post or cands
        ci = max(pool_c, key=lambda c: words_of(chapter_sents[c]) - budgets[c])
        if not drop_longest(ci):
            break

    # 3) Top up undershoot (via the climax chapter when the ratio is
    # already high, so topping up cannot push the climax out of band).
    guard = 0
    while total() < target_words * 0.93 and guard < 500:
        guard += 1
        ratio = climax_start() / total() if total() else 0.0
        room = int(target_words * 0.97) - total() + 6
        if ratio > 0.70:
            add_filler(climax_idx, max_words=room)
        else:
            pre = list(range(climax_idx)) or [0]
            add_filler(max(pre, key=lambda c: words_of(chapter_sents[c])), max_words=room)

    # 4) Final hard trim: never ship more than ~+18% over target when a
    # removable sentence exists (post-climax drops first — they also
    # raise the climax ratio; pre-climax drops are the last resort).
    guard = 0
    while total() > target_words * 1.18 and guard < 500:
        guard += 1
        cands = [ci for ci in range(len(chapter_sents)) if removable(ci)]
        if not cands:
            break
        post = [ci for ci in cands if ci >= climax_idx]
        ci = max(post or cands, key=lambda c: words_of(chapter_sents[c]))
        if not drop_longest(ci):
            break

    # 5) Joint enforcement: steps 1-4 can undo each other (trimming
    # pre-climax filler lowers the ratio; boosting the ratio grows the
    # total), so finish with one loop scored against the REAL Gate A
    # limits (ratio 0.65-0.75 measured at climax start + 2 words; word
    # cap +25%).  The best-scoring state seen is restored at the end,
    # and a repeated state ends the loop — the loop can therefore
    # never oscillate or make the bundle worse than its input.
    def _score() -> float:
        t = total()
        if t == 0:
            return float("inf")
        ratio = (climax_start() + 2) / t
        # Gate A's REAL hard limits come first: retention lint wants
        # the climax at 65-75% and the word count within ±25%.  A
        # state inside both always beats one outside either — the
        # tighter 66-74% band below is only a preference among
        # infeasible states, never a reason to keep a state the gate
        # itself would reject (e.g. 2 words over the cap at a 68%
        # climax beats a gate-passing 65.7% state on raw penalties).
        if 0.65 <= ratio <= 0.75 and target_words * 0.75 <= t <= target_words * 1.25:
            return abs(t - target_words) * 0.5
        score = 100000.0
        if ratio < 0.66:
            score += (0.66 - ratio) * 10000
        elif ratio > 0.74:
            score += (ratio - 0.74) * 10000
        if t > target_words * 1.24:
            score += (t - target_words * 1.24) * 10
        if t < target_words * 0.80:
            score += (target_words * 0.80 - t) * 10
        # Tiebreak (weaker than every hard violation above): among
        # gate-passing states prefer the one closest to the target.
        score += max(0.0, abs(t - target_words) - target_words * 0.15) * 0.5
        return score

    best_score = _score()
    best_state = [list(s) for s in chapter_sents]
    seen: set[tuple[int, ...]] = set()
    loop_dropped = False
    for _ in range(600):
        t = total()
        if t == 0:
            break
        key = tuple(words_of(s) for s in chapter_sents)
        if key in seen:
            break
        seen.add(key)
        ratio = (climax_start() + 2) / t
        over = t > target_words * 1.22
        if not over and 0.66 <= ratio <= 0.74:
            break
        post = [ci for ci in range(climax_idx, len(chapter_sents)) if removable(ci)]
        pre = [ci for ci in range(climax_idx) if removable(ci)]
        moved = False
        if ratio < 0.66:
            if over and post:
                moved = drop_longest(max(post, key=lambda c: words_of(chapter_sents[c])))
            elif t < target_words * 1.22:
                pre_all = list(range(climax_idx)) or [0]
                add_filler(
                    max(pre_all, key=lambda c: words_of(chapter_sents[c])),
                    max_words=int(target_words * 1.25) - t + 1,
                )
                moved = True
            elif post:
                moved = drop_longest(max(post, key=lambda c: words_of(chapter_sents[c])))
            elif (
                not loop_dropped
                and 0 < climax_idx < len(chapter_sents) - 1
                and len(chapter_sents[climax_idx]) >= 2
            ):
                # Last resort: drop the climax chapter's open-loop
                # line — the climax itself resolves into the
                # conclusion, and Gate A's sidecar does not need it.
                chapter_sents[climax_idx].pop()
                loop_dropped = True
                moved = True
        elif ratio > 0.74:
            if pre:
                moved = drop_longest(max(pre, key=lambda c: words_of(chapter_sents[c])))
        elif over:
            cands = [ci for ci in range(len(chapter_sents)) if removable(ci)]
            if cands:
                moved = drop_longest(
                    max(post or cands, key=lambda c: words_of(chapter_sents[c]))
                )
        if not moved:
            break
        score = _score()
        if score < best_score:
            best_score = score
            best_state = [list(s) for s in chapter_sents]
    chapter_sents[:] = [list(s) for s in best_state]


# ---------------------------------------------------------------------------
# build_content
# ---------------------------------------------------------------------------

_TITLE_SUFFIX_RU = [
    "Woh Haqeeqat Jo Hairan Kar Degi",
    "Poori Kahani",
    "Asal Sach Kya Hai?",
    "Sab Se Bara Raaz",
]
_TITLE_SUFFIX_EN = [
    "The Full Story",
    "What Nobody Tells You",
    "The Untold Truth",
]


def build_content(spec: Any, profile: NicheProfile) -> ContentBundle:
    """Build a complete, deterministic :class:`ContentBundle`.

    Same ``(spec, profile)`` input always yields the identical bundle:
    every choice is drawn from an RNG seeded by ``spec.seed`` + the
    topic hash.  Narration length targets the CALIBRATED word target
    from :func:`app.integration.planning.plan_word_target` (±20%):
    the selected voice's measured WPM (versioned calibration store)
    × ``spec.minutes`` — falling back to the profile's planning WPM,
    explicitly labelled ``unmeasured-default``, when no calibration
    data exists (Round 4; previously this was always
    ``profile.target_wpm * minutes`` at 110–160 WPM, which real Urdu
    edge-tts at ~199–225 WPM systematically overshot in speed).
    """
    rng = _seeded_rng(spec)
    topic = str(getattr(spec, "topic", "") or "").strip() or profile.name
    minutes_raw = getattr(spec, "minutes", 1.0)
    try:
        minutes = float(minutes_raw) if minutes_raw else 1.0
    except (TypeError, ValueError):
        minutes = 1.0
    if minutes <= 0:
        minutes = 1.0
    word_plan = plan_word_target(spec, profile, minutes=minutes)
    target_words = word_plan.target_words
    # Style Lab: a resolved profile's speech_wpm x target_duration_s
    # becomes the overall word target (the calibrated plan above is
    # the fallback when the profile carries no speech/duration
    # opinion).  The target is recorded on the bundle, so Gate A's
    # word-count machinery judges against it like any other plan.
    style = _resolve_style(spec)
    directives = style[1] if style else None
    if directives:
        styled_words = _style_target_words(directives)
        if styled_words is not None:
            target_words = styled_words
    mode = _language_mode(spec, profile)
    ru = mode != "english"

    niche_label = (
        str(getattr(spec, "custom_niche_name", None) or "").strip()
        or str(getattr(spec, "niche", "") or "").strip()
        or profile.name
    )
    kws = _keywords(topic)
    kw = kws[0] if kws else topic
    fmt = {"topic": topic, "kw": kw, "niche": niche_label}

    if directives:
        plan = _style_plan(profile, directives, target_words)
        roles = [role for _, role in plan]
        budgets = _style_budgets(roles, target_words, directives)
        script_style = _script_style_applied(
            directives, plan, budgets, target_words)
    else:
        plan = _plan_sections(profile, target_words)
        roles = [role for _, role in plan]
        budgets = _budgets(roles, target_words)
        script_style = None
    climax_idx = roles.index("climax")

    # Short format (< 140 words): use the concise sentence banks —
    # the standard banks' minimum chapter sizes cannot fit a
    # sub-1-minute target inside Gate A's word/ratio limits.
    short_format = target_words < 140
    if ru:
        hook_bank = _HOOK_RU_SHORT if short_format else _HOOK_RU
        body_bank = _BODY_RU_SHORT if short_format else _BODY_RU
        intro_bank = _INTRO_RU_SHORT if short_format else _INTRO_RU
        climax_bank = _CLIMAX_RU_SHORT if short_format else _CLIMAX_RU
        filler_bank = _FILLER_RU_SHORT if short_format else _FILLER_RU
        loop_bank = _OPEN_LOOP_RU
    else:
        hook_bank = _HOOK_EN_SHORT if short_format else _HOOK_EN
        body_bank = _BODY_EN_SHORT if short_format else _BODY_EN
        intro_bank = _INTRO_EN_SHORT if short_format else _INTRO_EN
        climax_bank = _CLIMAX_EN_SHORT if short_format else _CLIMAX_EN
        filler_bank = _FILLER_EN_SHORT if short_format else _FILLER_EN
        loop_bank = _OPEN_LOOP_EN

    def _compose(rng: random.Random):
        """One composition attempt on the given RNG stream."""
        hook = hook_bank[rng.randrange(len(hook_bank))].format(**fmt)

        def _pick(bank: list[str]) -> str:
            return bank[rng.randrange(len(bank))].format(**fmt)

        # Build each chapter as a sentence list: [primary, ...extras, closing]
        # where ``closing`` is protected — the open-loop line for non-final
        # chapters (retention rule 5) and the CTA for the conclusion.
        chapter_sents: list[list[str]] = []
        for i, ((section_title, role), budget) in enumerate(zip(plan, budgets)):
            is_last = i == len(plan) - 1
            if role == "hook":
                pool = [hook, _pick(intro_bank), _pick(body_bank)]
            elif role == "intro":
                pool = [_pick(intro_bank), _pick(body_bank)]
            elif role == "climax":
                if short_format:
                    # Single reveal line: take the tightest climax
                    # sentence — at sub-1-minute targets every extra
                    # post-climax word lowers the climax-start ratio.
                    pool = [
                        min(
                            (s.format(**fmt) for s in climax_bank),
                            key=_word_count,
                        ),
                        _pick(body_bank),
                    ]
                else:
                    pool = [_pick(climax_bank), _pick(body_bank)]
            elif role == "conclusion":
                if short_format:
                    if len(_words(topic)) > 4:
                        # Long topics inflate the fixed conclusion
                        # opener word-for-word; reference the topic
                        # by its head keyword so the post-climax
                        # mass stays inside the feasible band at
                        # tiny targets.
                        opener = (
                            f"To ye thi {kw} ki kahani."
                            if ru
                            else f"So that was the story of {kw}."
                        )
                    else:
                        opener = f"To ye tha {topic}." if ru else f"So that was {topic}."
                elif ru:
                    opener = f"To ye tha {topic} ka mukammal jaiza."
                else:
                    opener = f"So that was a complete look at {topic}."
                pool = [opener]
            else:  # main
                pool = [_pick(body_bank), _pick(body_bank)]
            if is_last:
                closing = str(profile.cta_style).strip()
                if short_format:
                    # Short format always uses the tight CTA: even a
                    # 12-13 word profile CTA is ~20% of a 60-word
                    # script, and that fixed post-climax mass is what
                    # decides whether the climax floor is reachable.
                    closing = _SHORT_CTA_RU if ru else _SHORT_CTA_EN
            elif role == "climax" and short_format:
                # Short format: the climax is a single reveal line and
                # resolves straight into the conclusion — no open-loop
                # padding after it (every post-climax word drags the
                # climax-start ratio below Gate A's 65% floor).
                closing = ""
            elif role == "climax" or target_words < 140:
                # Shortest open loop on the climax chapter (every word
                # after the climax pushes its start ratio down) and on
                # every chapter of a short-format video (mandatory blocks
                # must fit a ~1-minute script).
                closing = min(loop_bank, key=_word_count)
            else:
                closing = loop_bank[i % len(loop_bank)]
            chosen: list[str] = []
            total = _word_count(closing)
            for sentence in pool:
                words = _word_count(sentence)
                if not chosen or total + words <= budget:
                    chosen.append(sentence)
                    total += words
                if total >= budget:
                    break
            filler_idx = rng.randrange(len(filler_bank)) if filler_bank else 0
            guard = 0
            # Short-format climax stays a single reveal line: topping it
            # up to its (relatively larger) budget share would push the
            # post-climax word mass past what the 65% climax floor allows
            # at this total length.
            top_up = not (short_format and role == "climax")
            while top_up and total < budget and filler_bank and guard < 300:
                sentence = _sized_filler(filler_bank, fmt, filler_idx, budget - total + 4)
                filler_idx += 1
                guard += 1
                words = _word_count(sentence)
                if short_format and total + words > budget + 2:
                    # Tight chapters: a filler that overshoots the
                    # budget by more than a couple of words costs more
                    # (Gate A word cap) than the top-up is worth.
                    break
                chosen.append(sentence)
                total += words
            if closing:
                chosen.append(closing)
            chapter_sents.append(chosen)

        _balance_chapters(chapter_sents, budgets, climax_idx, target_words, filler_bank, fmt, rng)

        chapters: list[Chapter] = []
        for i, ((section_title, _role), chosen) in enumerate(zip(plan, chapter_sents)):
            title = section_title if len(section_title) <= 60 else section_title[:57].rstrip() + "..."
            chapters.append(Chapter(index=i + 1, title=title, text=" ".join(chosen).strip()))

        narration_text = "\n\n".join(c.text for c in chapters)
        script_text = "\n\n".join(f"## {c.title}\n{c.text}" for c in chapters)
        return hook, chapters, narration_text, script_text

    # Deterministic re-rolls: sentence-granular composition can
    # land a word or two outside Gate A's numeric limits (word
    # count within ±25%, climax at 65-75%) for unlucky seed/topic
    # pairs even when a feasible composition exists.  Re-compose
    # on salted streams (attempt 0 is the canonical stream) and
    # keep the first attempt satisfying both numeric limits; the
    # gate itself still runs unchanged on the returned bundle.
    hook = ""
    chapters: list[Chapter] = []
    narration_text = ""
    script_text = ""
    for attempt in range(8):
        rng = _seeded_rng(spec, attempt)
        hook, chapters, narration_text, script_text = _compose(rng)
        _total_words = _word_count(narration_text)
        _climax_start = sum(_word_count(c.text) for c in chapters[:climax_idx])
        _ratio = (_climax_start + 2) / _total_words if _total_words else 0.0
        if (
            0.65 <= _ratio <= 0.75
            and target_words * 0.75 <= _total_words <= target_words * 1.25
        ):
            break

    # -- Title / thumbnail / SEO pack (prototype 05_seo + 04_thumbnail) --
    suffixes = _TITLE_SUFFIX_RU if ru else _TITLE_SUFFIX_EN
    title_applied: dict | None = None
    if directives:
        title, title_applied = _style_title(
            topic, directives, rng, ru, len(chapters))
    else:
        title = f"{topic} | {suffixes[rng.randrange(len(suffixes))]}"
    thumb_words = [w.upper() for w in kws[:3]]
    while len(thumb_words) < 2:
        thumb_words.append("RAAZ" if ru else "REVEALED")
    if len(thumb_words) == 2:
        thumb_words.append("KHULA" if ru else "TRUTH")
    thumbnail_text = " ".join(thumb_words[:4])

    tags: list[str] = []
    for tag in [
        profile.profile_id.replace("_", " "),
        niche_label.lower(),
        topic.lower(),
        *[k.lower() for k in kws],
        "faceless video",
        "long video",
        "urdu" if ru else "documentary",
    ]:
        tag = tag.strip()
        if tag and tag not in tags:
            tags.append(tag)
    tags = tags[:12]

    chapter_lines = "\n".join(f"- Chapter {c.index}: {c.title}" for c in chapters)
    desc_applied: dict | None = None
    if directives:
        description, desc_applied = _style_description(
            topic, niche_label, chapters, profile, directives, ru, tags,
            channel_id=getattr(spec, "channel_id", None),
        )
    else:
        description = (
            f"{topic} — {niche_label} par mukammal video.\n\n"
            f"Is video mein aap jaanenge {topic} ki wo tafseelat jo zyada tar log nahi jaante.\n\n"
            f"Chapters:\n{chapter_lines}\n\n{profile.cta_style}"
            if ru
            else f"{topic} — a complete video on {niche_label}.\n\n"
            f"In this video you will learn the details of {topic} that most people never hear.\n\n"
            f"Chapters:\n{chapter_lines}\n\n{profile.cta_style}"
        )

    claims = _extract_claims(narration_text, chapters)

    style_applied: dict | None = None
    if directives:
        style_applied = {
            "script": script_style,
            "seo": {"title": title_applied, "description": desc_applied},
        }

    return ContentBundle(
        title=title,
        hook=hook,
        chapters=chapters,
        script_text=script_text,
        narration_text=narration_text,
        thumbnail_text=thumbnail_text,
        description=description,
        tags=tags,
        claims=claims,
        target_words=target_words,
        target_minutes=minutes,
        climax_chapter=climax_idx + 1,
        llm_path=LLM_PATH_STUB,
        planning_wpm=word_plan.wpm,
        planning_voice_id=word_plan.voice_id,
        planning_rate_pct=word_plan.rate_pct,
        wpm_provenance=word_plan.calibration.provenance.as_dict(),
        style_applied=style_applied,
    )


# ---------------------------------------------------------------------------
# Live Gemini path (Round 3)
# ---------------------------------------------------------------------------

def build_gemini_router(
    secret_provider: Any,
    *,
    ledger: Any = None,
    registry_path: Any = None,
    transport: Any = None,
    cap_checker: Any = None,
) -> Any:
    """Build the real :class:`LLMRouter` with ONLY the Gemini adapter.

    The router resolves tiers from ``config/models.yaml`` (the default
    hot-reloadable registry), enforces the Budget Guard caps through
    its cap checker (``cap_checker`` may inject the full guard; the
    router's default checker applies when omitted — either way the
    check is never bypassed) and writes every attempt to ``ledger``
    (an :class:`InMemoryLLMCallLedger` is created by the router when
    omitted).  Keys reach the adapter only via ``secret_provider``.
    """
    from pathlib import Path

    from app.providers.llm import GeminiAdapter, LLMRouter

    adapter = GeminiAdapter(secret_provider, GEMINI_KEY_REF, transport=transport)
    kwargs: dict[str, Any] = {}
    if ledger is not None:
        kwargs["ledger"] = ledger
    if cap_checker is not None:
        kwargs["cap_checker"] = cap_checker
    if registry_path is not None:
        return LLMRouter({"gemini": adapter}, registry=Path(registry_path), **kwargs)
    return LLMRouter({"gemini": adapter}, **kwargs)


async def gemini_key_available(secret_provider: Any) -> bool:
    """True when ``secret_provider`` resolves a non-empty Gemini key.

    Any provider-side failure (e.g. no keyring backend on a headless
    dev box) counts as "no key" — the stub path — never as a crash.
    """
    if secret_provider is None:
        return False
    try:
        key = await secret_provider.get_secret(GEMINI_KEY_REF)
    except Exception:
        return False
    return bool(key and str(key).strip())


async def resolve_llm_path(secret_provider: Any) -> str:
    """The provenance string the content stage will use for this run."""
    if await gemini_key_available(secret_provider):
        return LLM_PATH_GEMINI_LIVE
    return LLM_PATH_STUB


def _profile_layer_text(profile: NicheProfile, plan: Any = None) -> str:
    """Niche Profile Layer (Layer 2) text for prompt assembly."""
    lines = [
        f"Profile: {profile.name} ({profile.profile_id})",
        f"Language: {profile.language}",
        f"Tone: {', '.join(profile.tone)}",
        f"Target WPM: {profile.target_wpm}",
        f"Sentence style: {profile.sentence_style}",
        "Structure template: " + " | ".join(profile.structure_template),
        f"Hook families: {', '.join(profile.hook_families)}",
        f"CTA style: {profile.cta_style}",
        f"Voice style: {profile.voice_style}",
    ]
    if plan is not None:
        # Round 4: the word budgets in this prompt come from the
        # CALIBRATED plan (measured WPM for the selected voice+rate);
        # say so explicitly so the model is not pulled back toward the
        # profile's unmeasured planning constant above.
        lines.append(
            f"Planning WPM (calibrated): {plan.wpm:g} for voice "
            f"{plan.voice_id} at rate {plan.rate_pct:+.1f}% — "
            f"source: {plan.provenance.label()}. Chapter word budgets "
            f"below are computed from this WPM; follow them."
        )
    if profile.forbidden_patterns:
        lines.append("Forbidden patterns: " + "; ".join(profile.forbidden_patterns))
    if profile.special_rules:
        lines.append("Special rules: " + "; ".join(profile.special_rules))
    if profile.taboo:
        lines.append("Taboo — never include: " + "; ".join(profile.taboo))
    return "\n".join(lines)


def _strip_sidecar(text: str) -> tuple[str, dict | None]:
    """Split an agent response into (narration, sidecar dict | None).

    The script/outline contracts return narration first, then a fenced
    ```json sidecar.  The narration is everything before the fence;
    a trailing bare JSON object is also recognised.
    """
    import json

    raw = (text or "").strip()
    sidecar: dict | None = None
    narration = raw
    fence = raw.find("```")
    if fence != -1:
        narration = raw[:fence].strip()
        block = raw[fence + 3 :]
        if block.lstrip().lower().startswith("json"):
            block = block.lstrip()[4:]
        end = block.find("```")
        if end != -1:
            block = block[:end]
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, dict):
                sidecar = parsed
        except Exception:
            sidecar = None
    else:
        # Trailing bare JSON object after the prose.
        brace = raw.rfind("\n{")
        if brace != -1:
            try:
                parsed = json.loads(raw[brace + 1 :])
                if isinstance(parsed, dict):
                    sidecar = parsed
                    narration = raw[:brace].strip()
            except Exception:
                pass
    # Strip markdown chapter headings the model may have added inside
    # the narration part (the bundle re-adds its own headings).
    narration = re.sub(r"(?m)^#{1,3}\s+.*$", "", narration).strip()
    return narration, sidecar


def _split_final_pass(text: str, expected: int) -> list[str] | None:
    """Split a final-pass response on '## ' headings; None if unusable."""
    parts = re.split(r"(?m)^##\s+", text or "")
    bodies = [p.strip() for p in parts if p.strip()]
    if len(bodies) != expected:
        return None
    cleaned: list[str] = []
    for body in bodies:
        # Drop the heading line itself (first line) when present.
        lines = body.splitlines()
        if lines and _word_count(lines[0]) <= 12 and len(lines) > 1:
            body = "\n".join(lines[1:]).strip()
        narration, _sidecar = _strip_sidecar(body)
        if not narration:
            return None
        cleaned.append(narration)
    return cleaned


async def build_content_live(
    spec: Any,
    profile: NicheProfile,
    *,
    secret_provider: Any = None,
    llm: Any = None,
    ledger: Any = None,
    registry_path: Any = None,
    transport: Any = None,
    cap_checker: Any = None,
    project_id: str | None = None,
) -> ContentBundle:
    """Content stage with the key-driven live/stub switch (Round 3).

    * No Gemini key resolvable via ``secret_provider`` (or, when no
      provider is injected, via :func:`create_secret_provider` —
      keyring by default, env backend when ``YTA_SECRET_BACKEND=env``)
      → the deterministic bundle from :func:`build_content`,
      provenance ``"deterministic-stub"``.
    * Key resolvable → the real router (:func:`build_gemini_router`,
      unless a ready ``llm`` provider/router is injected) generates
      outline → chapters → final pass through
      :func:`app.agents.prompt_assembly.assemble_prompt`; provenance
      ``"gemini-live"`` with real models/usage/cost on the bundle.
      Router errors (bad key, budget cap, …) propagate unchanged.
    """
    if secret_provider is None and llm is None:
        from app.providers.secrets_env import create_secret_provider

        secret_provider = create_secret_provider()
    if secret_provider is not None and not await gemini_key_available(secret_provider):
        return build_content(spec, profile)
    if llm is None:
        llm = build_gemini_router(
            secret_provider,
            ledger=ledger,
            registry_path=registry_path,
            transport=transport,
            cap_checker=cap_checker,
        )

    from app.agents.prompt_assembly import PromptLayers, assemble_prompt, load_prompt
    from app.providers.llm.models import LLMRequest, Tier

    topic = str(getattr(spec, "topic", "") or "").strip() or profile.name
    minutes_raw = getattr(spec, "minutes", 1.0)
    try:
        minutes = float(minutes_raw) if minutes_raw else 1.0
    except (TypeError, ValueError):
        minutes = 1.0
    if minutes <= 0:
        minutes = 1.0
    word_plan = plan_word_target(spec, profile, minutes=minutes)
    target_words = word_plan.target_words
    niche_label = (
        str(getattr(spec, "custom_niche_name", None) or "").strip()
        or str(getattr(spec, "niche", "") or "").strip()
        or profile.name
    )
    kws = _keywords(topic)
    # Style Lab: same steering as the deterministic builder — the
    # profile's word target / chapter plan / budgets drive the
    # prompts, and a style-instruction block (format only, originality
    # mandatory) is appended to the outline and chapter prompts.
    style = _resolve_style(spec)
    directives = style[1] if style else None
    if directives:
        styled_words = _style_target_words(directives)
        if styled_words is not None:
            target_words = styled_words
        plan = _style_plan(profile, directives, target_words)
        roles = [role for _, role in plan]
        budgets = _style_budgets(roles, target_words, directives)
        script_style: dict | None = _script_style_applied(
            directives, plan, budgets, target_words)
        style_block = _style_instruction_block(
            directives, plan, budgets, target_words)
    else:
        plan = _plan_sections(profile, target_words)
        roles = [role for _, role in plan]
        budgets = _budgets(roles, target_words)
        script_style = None
        style_block = ""
    climax_idx = roles.index("climax")
    project = project_id or (
        "content-" + hashlib.sha256(f"{topic}|{getattr(spec, 'seed', 0)}".encode()).hexdigest()[:12]
    )
    channel_id = getattr(spec, "channel_id", None)

    niche_text = _profile_layer_text(profile, word_plan)
    channel_text = (
        f"Channel: {channel_id or 'local-test-channel'} — faceless long-form "
        "YouTube channel; narration only, no on-camera presenter."
    )
    topic_text = (
        f"Topic: {topic}\nNiche: {niche_label}\n"
        f"Keywords: {', '.join(kws) if kws else topic}\n"
        f"Language mode: {_language_mode(spec, profile)}"
    )

    results: list[Any] = []

    async def _call(
        agent_slug: str,
        task_name: str,
        tier: str,
        agent_id: str,
        task_id: str,
        layers: Any,
        max_output_tokens: int,
    ) -> Any:
        meta, _body = load_prompt(agent_slug, task_name)
        prompt = assemble_prompt(agent_slug, task_name, layers)
        req = LLMRequest(
            tier=Tier(tier),
            prompt=prompt,
            agent_id=agent_id,
            project_id=project,
            channel_id=str(channel_id) if channel_id else None,
            task=task_id,
            prompt_name=str(meta.get("name") or task_name),
            prompt_version=str(meta["version"]) if meta.get("version") else None,
            max_output_tokens=max_output_tokens,
            temperature=0.7,
        )
        result = await llm.generate(req)
        results.append(result)
        return result

    # -- 1. Outline (Agent 6, T1) ----------------------------------------
    plan_lines = "\n".join(
        f"- {i + 1}. {title} [{role}] — word budget {budget}"
        for i, ((title, role), budget) in enumerate(zip(plan, budgets))
    )
    outline_result = await _call(
        "outline",
        "outline",
        OUTLINE_TIER,
        "6",
        "outline",
        PromptLayers(
            niche_profile=niche_text,
            channel=channel_text,
            topic=topic_text,
            run_params=(
                f"Target minutes: {minutes}\nTarget words: {target_words}\n"
                f"Planned sections (from the niche structure template):\n{plan_lines}"
                + (f"\n\n{style_block}" if style_block else "")
            ),
        ),
        max_output_tokens=2048,
    )
    outline_text, _outline_sidecar = _strip_sidecar(outline_result.text)
    outline_ctx = (outline_text or plan_lines)[:3000]

    # -- 2. Chapters (Agent 7, T1, one call per chapter) ------------------
    chapter_texts: list[str] = []
    for i, ((section_title, role), budget) in enumerate(zip(plan, budgets)):
        result = await _call(
            "script_writing",
            "script_writing",
            CHAPTER_TIER,
            "7",
            "script.chapter",
            PromptLayers(
                niche_profile=niche_text,
                channel=channel_text,
                topic=f"{topic_text}\n\nOutline:\n{outline_ctx}",
                run_params=(
                    f"Chapter: {i + 1} of {len(plan)} — {section_title} "
                    f"(role: {role})\n"
                    f"Word budget: {budget} words "
                    f"(acceptable range {max(10, int(budget * 0.9))}-"
                    f"{int(budget * 1.1)})\n"
                    "Write ONLY this chapter's narration now."
                    + (f"\n\n{style_block}" if style_block else "")
                ),
            ),
            max_output_tokens=max(512, int(budget * 3)),
        )
        narration, _sidecar = _strip_sidecar(result.text)
        if not narration:
            raise RuntimeError(
                f"live chapter generation returned empty narration for "
                f"chapter {i + 1} ({section_title!r})"
            )
        chapter_texts.append(narration)

    # -- 3. Flagship final pass (Agent 7 final pass, T2 Pro-class) --------
    draft = "\n\n".join(
        f"## {title}\n{text}"
        for (title, _role), text in zip(plan, chapter_texts)
    )
    final_result = await _call(
        "script_writing",
        "script_writing",
        FINAL_PASS_TIER,
        "7",
        "script.final_pass",
        PromptLayers(
            niche_profile=niche_text,
            channel=channel_text,
            topic=topic_text,
            run_params=(
                "Final pass over the complete draft script below. Polish "
                "flow and language consistency ONLY — do not add new "
                "facts. Return the COMPLETE polished script, keeping "
                "the same chapters in the same order, each introduced "
                "by its '## <chapter title>' heading, narration only "
                f"under each heading.\n\nDraft script:\n{draft}"
            ),
        ),
        max_output_tokens=max(1024, target_words * 4),
    )
    polished = _split_final_pass(final_result.text, len(plan))
    if polished is not None:
        draft_words = sum(_word_count(t) for t in chapter_texts)
        polished_words = sum(_word_count(t) for t in polished)
        if draft_words and 0.5 * draft_words <= polished_words <= 1.6 * draft_words:
            chapter_texts = polished

    # -- Assemble the bundle (SEO scaffolding stays deterministic) -------
    stub = build_content(spec, profile)
    chapters = [
        Chapter(index=i + 1, title=title, text=text)
        for i, ((title, _role), text) in enumerate(zip(plan, chapter_texts))
    ]
    narration_text = "\n\n".join(c.text for c in chapters)
    script_text = "\n\n".join(f"## {c.title}\n{c.text}" for c in chapters)
    first = chapter_texts[0] if chapter_texts else ""
    hook = " ".join(re.split(r"(?<=[.!?])\s+", first)[:2]).strip() or first[:200]

    models: list[str] = []
    input_tokens = 0
    output_tokens = 0
    cost = 0.0
    for res in results:
        if res.model and res.model not in models:
            models.append(res.model)
        usage = res.usage
        input_tokens += int(
            getattr(usage, "input_uncached", 0)
            + getattr(usage, "input_cached", 0)
            + getattr(usage, "input_cache_write", 0)
        )
        output_tokens += int(
            getattr(usage, "output", 0) + getattr(usage, "reasoning", 0)
        )
        cost += float(res.cost_usd or 0.0)

    chapter_lines = "\n".join(f"- Chapter {c.index}: {c.title}" for c in chapters)
    desc_applied: dict | None = None
    if directives:
        description, desc_applied = _style_description(
            topic, niche_label, chapters, profile, directives,
            _language_mode(spec, profile) != "english", stub.tags,
            channel_id=getattr(spec, "channel_id", None),
        )
    else:
        description = (
            f"{topic} — {niche_label} par mukammal video.\n\n"
            f"Is video mein aap jaanenge {topic} ki wo tafseelat jo zyada tar log nahi jaante.\n\n"
            f"Chapters:\n{chapter_lines}\n\n{profile.cta_style}"
            if _language_mode(spec, profile) != "english"
            else f"{topic} — a complete video on {niche_label}.\n\n"
            f"In this video you will learn the details of {topic} that most people never hear.\n\n"
            f"Chapters:\n{chapter_lines}\n\n{profile.cta_style}"
        )
    style_applied: dict | None = None
    if directives:
        # Title formula was applied by the deterministic stub build
        # above (same spec/profile); the description record is this
        # path's own, built from the live chapters.
        stub_seo = (stub.style_applied or {}).get("seo") or {}
        style_applied = {
            "script": script_style,
            "seo": {
                "title": stub_seo.get("title"),
                "description": desc_applied,
            },
        }
    return ContentBundle(
        title=stub.title,
        hook=hook,
        chapters=chapters,
        script_text=script_text,
        narration_text=narration_text,
        thumbnail_text=stub.thumbnail_text,
        description=description,
        tags=stub.tags,
        claims=_extract_claims(narration_text, chapters),
        target_words=target_words,
        target_minutes=minutes,
        climax_chapter=climax_idx + 1,
        llm_path=LLM_PATH_GEMINI_LIVE,
        llm_models=models,
        llm_usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "calls": len(results),
        },
        llm_cost_usd=cost,
        planning_wpm=word_plan.wpm,
        planning_voice_id=word_plan.voice_id,
        planning_rate_pct=word_plan.rate_pct,
        wpm_provenance=word_plan.calibration.provenance.as_dict(),
        style_applied=style_applied,
    )


def build_content_auto(spec: Any, profile: NicheProfile, **kwargs: Any) -> ContentBundle:
    """Sync wrapper over :func:`build_content_live` for sync callers.

    Loop ownership (Round 4, ``app.integration.run_loop``): the live
    coroutine is driven via ``run_coroutine`` — on the produce run's
    single loop when called inside a run, or on a one-call boundary
    loop for a standalone sync caller; this wrapper owns no loop
    itself.  Raises ``RuntimeError`` when called from inside a running
    loop that is not the produce loop — async callers must
    ``await build_content_live(...)`` directly.
    """
    import asyncio

    from app.integration.run_loop import current_produce_loop, run_coroutine

    if current_produce_loop() is not None:
        return run_coroutine(
            lambda: build_content_live(spec, profile, **kwargs)
        )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run_coroutine(
            lambda: build_content_live(spec, profile, **kwargs)
        )
    raise RuntimeError(
        "build_content_auto() cannot run inside an active event loop; "
        "await build_content_live() instead"
    )


def _extract_claims(narration_text: str, chapters: list[Chapter]) -> list[dict]:
    """Fact-check claim candidates in the contract shape {text, type, chapter}.

    Uses the existing regex candidate extractor (candidates only — no
    verdicts are implied).  Each candidate is attributed to the chapter
    whose span in ``narration_text`` contains it.
    """
    spans: list[tuple[int, int, int]] = []
    pos = 0
    for chapter in chapters:
        start = narration_text.find(chapter.text, pos)
        if start < 0:
            start = pos
        spans.append((start, start + len(chapter.text), chapter.index))
        pos = start + len(chapter.text)

    def chapter_for(offset: int) -> int:
        for start, end, idx in spans:
            if start <= offset < end:
                return idx
        return spans[-1][2] if spans else 1

    claims: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for cand in extract_claim_candidates(narration_text):
        chapter_idx = chapter_for(int(cand.get("start", 0)))
        key = (str(cand["text"]), chapter_idx)
        if key in seen:
            continue
        seen.add(key)
        claims.append(
            {
                "text": str(cand["text"]),
                "type": str(cand.get("suggested_type") or cand.get("type") or "statistic"),
                "chapter": chapter_idx,
            }
        )
    return claims


# ---------------------------------------------------------------------------
# Gate A
# ---------------------------------------------------------------------------

_INTERRUPT_CYCLE = [
    "visual-change",
    "zoom",
    "on-screen-text",
    "music-shift",
    "silence-beat",
]


def _claim_type(value: Any) -> ClaimType:
    try:
        return ClaimType(str(value))
    except ValueError:
        return ClaimType.STATISTIC


def _build_sidecar(bundle: ContentBundle, profile: NicheProfile) -> ScriptSidecar:
    """Build the retention sidecar for ``bundle`` from its real word spans."""
    total = _word_count(bundle.narration_text)
    spans: list[ChapterSpan] = []
    markers: list[InterruptMarker] = []
    cursor = 0
    # Retention spacing is in *delivered-time* units (~0.35 min of
    # narration, 90 s max gap in the lint), so it must use the WPM the
    # bundle was PLANNED at — the calibrated planning WPM recorded on
    # the bundle (Round 4) — not the profile's unmeasured constant.
    # Bundles built outside this module carry no planning WPM and keep
    # the historical profile-constant behaviour.
    planning_wpm = float(getattr(bundle, "planning_wpm", 0) or 0)
    effective_wpm = planning_wpm if planning_wpm > 0 else float(profile.target_wpm)
    step_base = max(18, int(round(effective_wpm * 0.35)))
    for i, chapter in enumerate(bundle.chapters):
        count = _word_count(chapter.text)
        start, end = cursor, cursor + count
        spans.append(
            ChapterSpan(
                chapter_id=f"ch{chapter.index:02d}",
                start_word=start,
                end_word=end,
                ends_on_unresolved=(i < len(bundle.chapters) - 1),
                interrupt_kinds=[_INTERRUPT_CYCLE[i % len(_INTERRUPT_CYCLE)]],
            )
        )
        # Varied production markers inside the chapter (~every 0.35 min
        # of words, spacing jittered deterministically by chapter index).
        pos = start + step_base + (i % 3) * 7
        while pos < end - 4:
            markers.append(
                InterruptMarker(
                    position_words=pos,
                    kind=_INTERRUPT_CYCLE[(i + 1) % len(_INTERRUPT_CYCLE)],
                )
            )
            pos += step_base + ((pos // 7) % 3) * 6
        cursor = end
    # Climax position: start of the climax chapter (+2 words in), which
    # build_content budgets to begin at ~65% of the narration.
    climax_word: int | None = None
    if bundle.climax_chapter and 1 <= bundle.climax_chapter <= len(spans):
        climax_word = spans[bundle.climax_chapter - 1].start_word + 2
    elif total:
        climax_word = int(round(total * 0.70))
    hook_text = (bundle.hook or "").lower()
    greeting = bool(
        re.search(
            r"\b(hello|hi everyone|welcome|assalam|namaste|hey guys)\b", hook_text
        )
    )
    return ScriptSidecar(
        total_words=total,
        target_wpm=int(round(effective_wpm)),
        chapters=spans,
        markers=markers,
        climax_word=climax_word,
        hook_first_30s_has_proof=bool(bundle.hook and _word_count(bundle.hook) >= 8),
        hook_has_greeting=greeting,
    )


def run_gate_a(
    bundle: ContentBundle, profile: NicheProfile, spec: Any | None = None
) -> tuple[bool, list[str]]:
    """Run Gate A over ``bundle``.  Returns ``(passed, reasons)``.

    Checks, via the existing services:

    1. Fact-check: every claim candidate in the bundle is evaluated
       with :func:`app.agents.factcheck.evaluate_claim` (as a
       non-load-bearing candidate with no sources yet — YELLOW, not a
       blocker).  A claim that arrives with an explicit RED verdict,
       or that evaluates RED (e.g. marked load-bearing / contradicted),
       fails the gate.
    2. Retention lint: :func:`app.agents.retention.lint_retention` must
       have no ``fail`` issues.
    3. Gate bookkeeping: :func:`app.orchestration.gates.can_pass_gate_a`
       over the state implied by (1) and (2).
    4. Word count: narration within ±25% of the target
       (the calibrated word target; minutes from ``spec`` when
       given, else from the bundle's recorded target).
    """
    reasons: list[str] = []

    # -- 1. Fact-check verdicts -------------------------------------------
    evaluated: list[Claim] = []
    for i, raw in enumerate(bundle.claims or []):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        chapter = raw.get("chapter", 1)
        pre_verdict = raw.get("verdict")
        if isinstance(pre_verdict, str) and pre_verdict.upper() == Verdict.RED.value:
            reasons.append(f"RED claim in chapter {chapter}: {text!r} (pre-marked RED)")
            continue
        claim = Claim(
            claim_id=f"c{i + 1:03d}",
            text=text,
            claim_type=_claim_type(raw.get("type")),
            script_location=f"ch{int(chapter):02d}" if str(chapter).isdigit() else str(chapter),
            load_bearing=bool(raw.get("load_bearing", False)),
            sources=[],
            contradicted=bool(raw.get("contradicted", False)),
        )
        evaluated.append(evaluate_claim(claim))
    ledger = FactLedger(project_id="gate-a", claims=evaluated)
    red_count = ledger.red_count()
    reasons.extend(ledger.gate_a_blockers())

    # -- 2. Retention lint --------------------------------------------------
    sidecar = _build_sidecar(bundle, profile)
    issues = lint_retention(sidecar)
    retention_ok = retention_lint_passed(issues)
    for issue in issues:
        if issue.severity == "fail":
            reasons.append(f"Retention lint [{issue.rule}] {issue.location}: {issue.message}")

    # -- 3. Gate bookkeeping via the orchestration gate ---------------------
    state = {
        "locked_script_ref": "content-bundle" if bundle.script_text else None,
        "locked_script_hash": hashlib.sha256(
            bundle.narration_text.encode("utf-8")
        ).hexdigest(),
        "script_versions": ["v1"] if bundle.script_text else [],
        "red_claim_count": red_count,
        "retention_lint_passed": retention_ok,
    }
    gate_ok, gate_reasons = can_pass_gate_a(state)
    if not gate_ok:
        for reason in gate_reasons:
            if reason not in reasons:
                reasons.append(reason)

    # -- 4. Word count within ±25% -------------------------------------------
    target = int(bundle.target_words or 0)
    if not target and spec is not None:
        minutes = getattr(spec, "minutes", None)
        if minutes:
            # Same calibrated planner as the builders (Round 4), so a
            # bundle without a recorded target is judged against the
            # measured-WPM plan, not the old planning constant.
            target = plan_word_target(spec, profile, minutes=float(minutes)).target_words
    if target > 0:
        actual = _word_count(bundle.narration_text)
        low, high = target * 0.75, target * 1.25
        if not (low <= actual <= high):
            reasons.append(
                f"Word count {actual} outside ±25% of target {target} "
                f"(allowed {int(low)}-{int(high)})"
            )
    else:
        reasons.append("Word-count target unknown (no spec minutes / bundle target)")

    # Structural sanity: hook present and greeting-free, chapters 3-5
    # for the short test format is advisory only — never a hard fail
    # for longer formats, so it is not appended to reasons.

    return (not reasons, reasons)
