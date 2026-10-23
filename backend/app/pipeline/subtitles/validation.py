"""Cue-offset validation and script-match checks (QC gates).

* Offset gate (roadmap Part 6.7): sampled cue offsets must be
  <= :data:`OFFSET_TOLERANCE_MS` (200 ms) against reference word timings
  (alignment or whisper).  :func:`check_cue_offsets` compares every cue's
  start against the reference start of its first word and returns a full
  report — pass/fail is never silent.
* Script-match gate: Roman-script caption profiles (``roman_urdu``,
  ``english``) require >= 98% match against the original script
  (:data:`ROMAN_MIN_SCRIPT_MATCH`); the same 98% bar is applied to
  ``urdu_script`` text match.  The ratio itself is computed by
  :func:`script_match_ratio` so other stages can reuse it.
* Fair metric (R4-005): :func:`check_script_match` also computes the
  transliteration-aware :func:`script_match_ratio_fair`, and production
  QC reports carry BOTH scores, each named via
  :data:`STRICT_SCRIPT_MATCH_METRIC` / :data:`FAIR_SCRIPT_MATCH_METRIC`.
  Per :data:`SUBTITLE_SCRIPT_MATCH_GATE_POLICY` the fair score never
  decides a gate outcome.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional, Sequence

from .models import (
    ROMAN_PROFILES,
    CaptionsProfile,
    Cue,
    WordTiming,
    coerce_word_timings,
)

#: QC bar: a cue starting more than this far from its reference word fails.
OFFSET_TOLERANCE_MS = 200.0

#: Minimum script-match ratio for Roman-script caption profiles.
ROMAN_MIN_SCRIPT_MATCH = 0.98

#: Minimum script-match ratio per captions profile.
MIN_SCRIPT_MATCH_BY_PROFILE: dict[CaptionsProfile, float] = {
    CaptionsProfile.ROMAN_URDU: ROMAN_MIN_SCRIPT_MATCH,
    CaptionsProfile.ENGLISH: ROMAN_MIN_SCRIPT_MATCH,
    CaptionsProfile.URDU_SCRIPT: 0.98,
}

#: Canonical names of the two subtitle script-match metrics.  Every QC
#: report that carries a subtitle score names its metric with one of
#: these strings — a bare number is never a valid subtitle score in a
#: production report (R4-005).
STRICT_SCRIPT_MATCH_METRIC = "script_match_ratio"
FAIR_SCRIPT_MATCH_METRIC = "script_match_ratio_fair"

#: Gate policy for subtitle script-match scores (R4-005, named so the
#: rule is explicit rather than implicit): both scores are REPORT-ONLY.
#: The strict score alone drives :attr:`ScriptMatchReport.passed`
#: against :data:`MIN_SCRIPT_MATCH_BY_PROFILE`; the fair
#: (transliteration-aware) score is reported alongside it for context —
#: including in subtitle WARN messages — and is NOT an input to any
#: PASS/WARN/BLOCK decision.  Gate B
#: (``app.pipeline.gates.conditional.classify_qc``) classifies QC
#: verdicts + reason-code flags only and never reads subtitle scores.
#: Changing this policy is a deliberate, separately-reviewed change;
#: the default preserves the pre-Round-5 behaviour exactly.
SUBTITLE_SCRIPT_MATCH_GATE_POLICY = "report_only"

_TOKEN_STRIP_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_token(token: str) -> str:
    """Case-folded, punctuation-stripped token for matching purposes."""
    return _TOKEN_STRIP_RE.sub("", token).casefold()


def normalize_text(text: str) -> str:
    """Normalise text for comparison: NFC, case-folded, punctuation removed,
    whitespace collapsed.  Script characters themselves are preserved."""
    decomposed = unicodedata.normalize("NFC", text or "")
    kept = "".join(
        ch for ch in decomposed if not unicodedata.category(ch).startswith("P")
    )
    return " ".join(kept.casefold().split())


def script_match_ratio(reference: str, candidate: str) -> float:
    """Similarity of ``candidate`` against the reference script, in [0, 1].

    Token-sequence ratio (difflib) over normalised text, so capitalisation
    and punctuation differences don't count against a caption, while word
    substitutions / omissions / reorderings do.  Identical text scores 1.0.
    """
    ref_tokens = normalize_text(reference).split()
    cand_tokens = normalize_text(candidate).split()
    if not ref_tokens and not cand_tokens:
        return 1.0
    if not ref_tokens or not cand_tokens:
        return 0.0
    return SequenceMatcher(a=ref_tokens, b=cand_tokens, autojunk=False).ratio()


def _is_arabic_script_char(ch: str) -> bool:
    return (
        "\u0600" <= ch <= "\u06ff"
        or "\u0750" <= ch <= "\u077f"
        or "\ufb50" <= ch <= "\ufdff"
        or "\ufe70" <= ch <= "\ufeff"
    )


def _is_latin_script_char(ch: str) -> bool:
    return (
        "a" <= ch.lower() <= "z"
        or "\u00c0" <= ch <= "\u024f"
        or "\u1e00" <= ch <= "\u1eff"
    )


def character_script_ratio(text: str, profile: CaptionsProfile | str) -> float:
    """Share of alphabetic characters written in the profile's script.

    ``urdu_script`` expects Arabic-script characters; the Roman profiles
    expect Latin characters.  Returns 1.0 for empty/no-letter text.
    """
    profile = CaptionsProfile.coerce(profile)
    letters = [ch for ch in (text or "") if ch.isalpha()]
    if not letters:
        return 1.0
    if profile is CaptionsProfile.URDU_SCRIPT:
        hits = sum(1 for ch in letters if _is_arabic_script_char(ch))
    else:
        hits = sum(1 for ch in letters if _is_latin_script_char(ch))
    return hits / len(letters)


@dataclass(frozen=True)
class ScriptMatchReport:
    profile: CaptionsProfile
    ratio: float
    threshold: float
    passed: bool
    char_script_ratio: float
    #: Transliteration-aware ("fair") score for the same text pair,
    #: computed by :func:`script_match_ratio_fair`.  ``None`` only for
    #: reports constructed outside :func:`check_script_match`.
    ratio_fair: Optional[float] = None
    #: Metric names for the two scores (see the module constants).
    strict_metric: str = STRICT_SCRIPT_MATCH_METRIC
    fair_metric: str = FAIR_SCRIPT_MATCH_METRIC

    @property
    def is_roman_profile(self) -> bool:
        return self.profile in ROMAN_PROFILES

    def metrics_dict(self) -> dict[str, Any]:
        """Both scores, each paired with its metric name (R4-005).

        This is the shape production QC reports embed, so a reader can
        never mistake a fair score for the strict one (or vice versa).
        ``passed`` reflects the STRICT score only — see
        :data:`SUBTITLE_SCRIPT_MATCH_GATE_POLICY`.
        """
        return {
            "strict": {"metric": self.strict_metric, "score": self.ratio},
            "fair": {"metric": self.fair_metric, "score": self.ratio_fair},
            "threshold": self.threshold,
            "passed": self.passed,
            "gate_policy": SUBTITLE_SCRIPT_MATCH_GATE_POLICY,
        }


def check_script_match(
    script_text: str,
    caption_text: str,
    profile: CaptionsProfile | str,
) -> ScriptMatchReport:
    """Check caption text against the original script for ``profile``.

    Roman-caption profiles require >= 98% script-match; the check ships for
    every profile with its threshold taken from
    :data:`MIN_SCRIPT_MATCH_BY_PROFILE`.

    Both metrics are computed and carried on the report: the strict
    :func:`script_match_ratio` (``ratio``) and the transliteration-aware
    :func:`script_match_ratio_fair` (``ratio_fair``).  ``passed`` is
    decided by the STRICT score alone, exactly as before Round 5 — the
    fair score is report/WARN context only
    (:data:`SUBTITLE_SCRIPT_MATCH_GATE_POLICY`).
    """
    profile = CaptionsProfile.coerce(profile)
    ratio = script_match_ratio(script_text, caption_text)
    ratio_fair = script_match_ratio_fair(script_text, caption_text)
    threshold = MIN_SCRIPT_MATCH_BY_PROFILE[profile]
    return ScriptMatchReport(
        profile=profile,
        ratio=ratio,
        threshold=threshold,
        passed=ratio >= threshold,
        char_script_ratio=character_script_ratio(caption_text, profile),
        ratio_fair=ratio_fair,
    )


# ---------------------------------------------------------------------------
# Transliteration-aware ("fair") script match
#
# Added in Round 4 (subtitle-metric honesty).  The original
# :func:`script_match_ratio` above is the RAW metric and is unchanged: it
# compares normalised text token-by-token in the ORIGINAL scripts, so a
# Roman Urdu reference compared against Whisper captions emitted in Urdu
# script scores ~0.0 even when the words agree — a script artefact, not a
# transcription measurement.  The functions below normalise BOTH sides
# into a common Roman representation before applying the SAME scoring, so
# the two numbers can be reported side by side.  The fair score measures
# script-normalised textual agreement only; it is NOT a transcription-
# correctness (WER) measurement against the audio.
# ---------------------------------------------------------------------------

#: Arabic-script diacritics / marks stripped before transliteration:
#: tatweel, tanwin + harakat (fatha/damma/kasra, shadda, sukun), madda /
#: hamza marks, superscript alef, and the Qur'anic annotation marks.
_URDU_DIACRITICS = frozenset(
    "ـًٌٍَُِّْٰٕٓٔۖۗۘۙۚۛۜ۝۞ۣ۟۠ۡۢۤۥۦۧۨ۩۪ۭ۫۬"
)

#: Single-character Urdu/Arabic letters -> Roman equivalents.
#: This is a deliberately SIMPLIFIED phonetic mapping: letters that are
#: distinct in Urdu script but collapse to one sound in Roman Urdu share
#: an equivalent (ث/ص -> s, ذ/ض/ظ -> z, ط -> t, ح/ہ -> h), and the
#: retroflexes ٹ/ڈ/ڑ map to their plain t/d/r counterparts, matching how
#: Roman Urdu is actually typed.  Short vowels are NOT in this table:
#: Urdu script normally omits zabar/zer/pesh, so they cannot be
#: recovered by transliteration at all (see the function docstring).
_URDU_CHAR_MAP: dict[str, str] = {
    "ا": "a", "آ": "a", "أ": "a", "إ": "a", "ٱ": "a",
    "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s",
    "ج": "j", "چ": "ch", "ح": "h", "خ": "kh",
    "د": "d", "ڈ": "d", "ذ": "z",
    "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh",
    "س": "s", "ش": "sh", "ص": "s", "ض": "z",
    "ط": "t", "ظ": "z", "ع": "a", "غ": "gh",
    "ف": "f", "ق": "q", "ک": "k", "ك": "k", "گ": "g",
    "ل": "l", "م": "m", "ن": "n", "ں": "n",
    "ہ": "h", "ه": "h", "ۃ": "h", "ة": "h", "ۂ": "h",
    "ؤ": "w", "ء": "",
    "۔": ".", "؟": "?", "،": ",",
}

#: Codepoints of ی (Farsi yeh U+06CC and Arabic yeh U+064A), whose Roman
#: value is contextual: "y" before a vowel letter, "e" before noon
#: ghunna, "i" elsewhere (word-final or before a consonant).
_URDU_YEH = frozenset("یی")
#: Codepoints of و (waw), also contextual — see transliterate function.
_URDU_WAW = frozenset("و")
_URDU_NOON_GHUNNA = "ں"
_URDU_ALEF = frozenset("اآ")
#: Letters after which ی is read as the consonant "y" and و as "w".
_URDU_VOWEL_LETTERS = frozenset("اآوےۓؤ")

#: Whole-word exceptions: a handful of ultra-frequent function words in
#: which ہ is a silent vowel carrier, so the per-character mapping would
#: produce a consonant skeleton no Roman Urdu writer would type
#: (کہ -> "kh" instead of "ke").  Kept deliberately tiny and explicit.
_URDU_WORD_EXCEPTIONS: dict[str, str] = {
    "کہ": "ke",
    "یہ": "ye",
    "وہ": "wo",
    "نہ": "na",
}


def _is_urdu_script_char(ch: str) -> bool:
    return _is_arabic_script_char(ch)


def _transliterate_urdu_run(run: str) -> str:
    """Transliterate one maximal run of Urdu-script letters."""
    exception = _URDU_WORD_EXCEPTIONS.get(run)
    if exception is not None:
        return exception
    out: list[str] = []
    for i, ch in enumerate(run):
        prev = run[i - 1] if i > 0 else ""
        nxt = run[i + 1] if i + 1 < len(run) else ""
        if ch in _URDU_YEH:
            if nxt == _URDU_NOON_GHUNNA:
                out.append("e")  # میں / ہیں: یں is a nasalised "e/ai"
            elif not nxt:
                out.append("i")  # word-final ی: کی -> ki, جاتی -> jati
            elif nxt in _URDU_VOWEL_LETTERS:
                out.append("y")  # کیا -> kya, گیا -> gya
            else:
                out.append("i")  # before a consonant: ایک -> aik
        elif ch in _URDU_WAW:
            if i == 0:
                out.append("w")  # word-initial و: وہ (exception), وقت
            elif not nxt:
                out.append("o")  # word-final و: تو/جو/کو -> to/jo/ko
            elif prev in _URDU_ALEF and i == 1:
                out.append("u")  # اور -> aur
            elif nxt in _URDU_VOWEL_LETTERS:
                out.append("w")
            else:
                out.append("o")  # medial و before a consonant is a vowel
        elif ch in "ےۓ":  # bari ye: the "e/ai" sound — کے، ہے، جانتے
            out.append("e")
        elif ch == "ئ":  # yeh hamza: گئی، آئی
            out.append("y")
        elif ch == "ھ":  # heh doachashmee: aspiration of the prior letter
            out.append("h")  # بھ -> bh, پھ -> ph, چھ -> chh, …
        elif ch == "ی" or ch == "ى":  # (ى alef maksura, rare in Urdu)
            out.append("i")
        elif ch in _URDU_CHAR_MAP:
            out.append(_URDU_CHAR_MAP[ch])
        elif "٠" <= ch <= "٩":  # Arabic-Indic digits
            out.append(str(ord(ch) - 0x0660))
        elif "۰" <= ch <= "۹":  # Extended Arabic-Indic (Urdu) digits
            out.append(str(ord(ch) - 0x06F0))
        elif ch.isdigit():
            out.append(ch)
        # Any other Arabic-script letter (rare loan / regional letters
        # with no entry above) is dropped — see the public docstring.
    return "".join(out)


def transliterate_urdu_to_roman(text: str) -> str:
    """Transliterate Urdu-script text into Roman (Latin) characters.

    This is a **simplified phonetic transliteration**, not a scholarly
    one.  It maps the Urdu alphabet (including the retroflexes
    ٹ/ڈ/ڑ, the aspirate marker ھ in digraphs such as بھ/پھ/ٹھ,
    the bari ye ے, noon ghunna ں, and common Arabic loan letters
    such as ث/ص/ض/ط/ظ/ع/غ) plus Arabic-Indic and Urdu digits to the
    Roman spellings a Roman Urdu writer would most commonly type, and
    strips Arabic diacritics (zabar/zer/pesh, tanwin, shadda, sukun,
    tatweel, …).  Characters that are already Latin (or digits, spaces,
    punctuation) pass through unchanged.

    Known limitations, by design: Urdu script normally OMITS short
    vowels, so transliteration recovers the consonant skeleton plus
    long vowels only (پر -> "pr", not "par"; سب -> "sb", not "sab");
    و and ی are genuinely ambiguous and are resolved by the simple
    contextual rules in :func:`_transliterate_urdu_run`; ع is flattened
    to "a".  Scores built on this transliteration therefore UNDERSTATE
    true word agreement — they are a fairer floor, not an exact match.
    """
    if not text:
        return ""
    stripped = "".join(ch for ch in text if ch not in _URDU_DIACRITICS)
    parts: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            parts.append(_transliterate_urdu_run("".join(run)))
            run.clear()

    for ch in stripped:
        if _is_urdu_script_char(ch):
            run.append(ch)
        else:
            flush()
            parts.append(ch)
    flush()
    return "".join(parts)


_DOUBLED_LETTER_RE = re.compile(r"(.)\1+")


def _phonetic_fold(token: str) -> str:
    """Symmetric phonetic foldings applied to BOTH sides of the fair
    comparison: doubled letters collapse (``aap`` == آپ ``ap``), the
    Roman digraph spellings ``ai``/``ei``/``ay`` of the sound written
    ے/یں in Urdu script fold to ``e`` (``hai`` == ہے ``he``,
    ``mein`` == میں ``men``), and ``v`` folds to ``w``.  Tokens made of
    digits are returned unchanged — collapsing ``1992`` to ``192`` (or
    ``200``/``2000`` to the same token) would corrupt, or falsely
    equate, numbers."""
    if token.isdigit():
        return token
    folded = _DOUBLED_LETTER_RE.sub(r"\1", token)
    folded = folded.replace("ai", "e").replace("ei", "e").replace("ay", "e")
    return folded.replace("v", "w")


def normalise_for_match(text: str) -> str:
    """Normalise text into the common representation used by the fair
    metric: transliterate to Roman if the text contains Urdu-script
    characters, then apply the SAME base normalisation as the raw
    metric (:func:`normalize_text` — NFC, punctuation removed,
    case-folded, whitespace collapsed), then the symmetric phonetic
    foldings of :func:`_phonetic_fold` token by token."""
    source = text or ""
    if any(_is_urdu_script_char(ch) for ch in source):
        source = transliterate_urdu_to_roman(source)
    base = normalize_text(source)
    return " ".join(_phonetic_fold(tok) for tok in base.split())


def script_match_ratio_fair(reference: str, candidate: str) -> float:
    """"Fair" similarity of ``candidate`` against the reference, in [0, 1].

    Both sides are first normalised with :func:`normalise_for_match`
    (Urdu-script text is transliterated to Roman, so a Roman Urdu
    reference and Urdu-script Whisper captions are compared in one
    script); the similarity itself is then computed EXACTLY as in
    :func:`script_match_ratio` — a difflib token-sequence ratio over
    the normalised tokens, with the same empty-input edges (both empty
    -> 1.0, either empty -> 0.0).  That function's internals are
    mirrored here rather than refactored, so the raw metric's behaviour
    is untouched.

    Report this alongside the raw :func:`script_match_ratio`, never
    instead of it: the fair score measures script-normalised textual
    agreement, NOT transcription correctness against the audio.
    """
    ref_tokens = normalise_for_match(reference).split()
    cand_tokens = normalise_for_match(candidate).split()
    if not ref_tokens and not cand_tokens:
        return 1.0
    if not ref_tokens or not cand_tokens:
        return 0.0
    return SequenceMatcher(a=ref_tokens, b=cand_tokens, autojunk=False).ratio()


# ---------------------------------------------------------------------------
# Offset validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OffsetViolation:
    cue_index: int
    cue_start: float
    reference_start: float
    offset_ms: float
    cue_text: str


@dataclass(frozen=True)
class OffsetReport:
    threshold_ms: float
    checked_cues: int
    max_offset_ms: float
    mean_offset_ms: float
    violations: tuple[OffsetViolation, ...] = ()
    unchecked_cue_indexes: tuple[int, ...] = ()

    @property
    def passed(self) -> bool:
        """PASS only when every *checked* cue is within the threshold.

        Cues that could not be matched to a reference word are reported in
        ``unchecked_cue_indexes`` rather than silently counted as passes.
        """
        return not self.violations

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


def check_cue_offsets(
    cues: Sequence[Cue],
    reference_word_timings: Iterable[Any],
    *,
    threshold_ms: float = OFFSET_TOLERANCE_MS,
) -> OffsetReport:
    """Compare each cue's start to the reference timing of its first word.

    Reference words are matched sequentially (cues and references are both
    in timeline order): for each cue, the next reference word whose
    normalised token equals the cue's first token is the anchor.  The
    offset is ``abs(cue.start - reference.start)`` in milliseconds; cues
    exceeding ``threshold_ms`` (default 200 ms) are violations.
    """
    reference: list[WordTiming] = coerce_word_timings(reference_word_timings)
    violations: list[OffsetViolation] = []
    unchecked: list[int] = []
    offsets: list[float] = []
    pointer = 0
    # Bounded look-ahead so one missing word doesn't consume the reference.
    lookahead = 64

    for cue in cues:
        tokens = cue.text.split()
        first = normalize_token(tokens[0]) if tokens else ""
        found: Optional[int] = None
        if first:
            upper = min(len(reference), pointer + lookahead)
            for idx in range(pointer, upper):
                if normalize_token(reference[idx].word) == first:
                    found = idx
                    break
        if found is None:
            unchecked.append(cue.index)
            continue
        ref_word = reference[found]
        pointer = found + 1
        offset_ms = abs(cue.start - ref_word.start) * 1000.0
        offsets.append(offset_ms)
        if offset_ms > threshold_ms:
            violations.append(
                OffsetViolation(
                    cue_index=cue.index,
                    cue_start=cue.start,
                    reference_start=ref_word.start,
                    offset_ms=offset_ms,
                    cue_text=cue.text,
                )
            )

    return OffsetReport(
        threshold_ms=threshold_ms,
        checked_cues=len(offsets),
        max_offset_ms=max(offsets) if offsets else 0.0,
        mean_offset_ms=(sum(offsets) / len(offsets)) if offsets else 0.0,
        violations=tuple(violations),
        unchecked_cue_indexes=tuple(unchecked),
    )
