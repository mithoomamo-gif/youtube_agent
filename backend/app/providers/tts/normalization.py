"""Text normalization — runs BEFORE the pronunciation dictionary.

Passes (roadmap sections 30 + 10):
1. strip markdown / emoji / stage directions / URLs
2. numbers, dates, times, currency, percent and abbreviations -> spoken words
   per language (full English implementation; Urdu/Hindi digit-words use the
   South-Asian lakh/crore grouping)
3. Urdu-script punctuation + ZWNJ handling
4. flag ALL-CAPS / Latin-script tokens inside Urdu/Hindi text into
   ``flagged_tokens`` (for the review UI / dictionary lookup queue)
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# English number words
# ---------------------------------------------------------------------------
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
         "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]
_ORDINAL = {
    "zero": "zeroth", "one": "first", "two": "second", "three": "third",
    "four": "fourth", "five": "fifth", "six": "sixth", "seven": "seventh",
    "eight": "eighth", "nine": "ninth", "ten": "tenth", "eleven": "eleventh",
    "twelve": "twelfth", "thirteen": "thirteenth", "fourteen": "fourteenth",
    "fifteen": "fifteenth", "sixteen": "sixteenth", "seventeen": "seventeenth",
    "eighteen": "eighteenth", "nineteen": "nineteenth", "twenty": "twentieth",
    "thirty": "thirtieth", "forty": "fortieth", "fifty": "fiftieth",
    "sixty": "sixtieth", "seventy": "seventieth", "eighty": "eightieth",
    "ninety": "ninetieth", "hundred": "hundredth", "thousand": "thousandth",
    "million": "millionth", "billion": "billionth",
}


def _under_thousand_en(n: int) -> str:
    parts: list[str] = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} hundred")
        n %= 100
    if n >= 20:
        word = _TENS[n // 10]
        if n % 10:
            word += f"-{_ONES[n % 10]}"
        parts.append(word)
    elif n > 0 or not parts:
        parts.append(_ONES[n])
    return " ".join(parts)


def number_to_words_en(n: int) -> str:
    if n == 0:
        return "zero"
    if n < 0:
        return "minus " + number_to_words_en(-n)
    scales = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]
    parts: list[str] = []
    for value, name in scales:
        if n >= value:
            parts.append(f"{_under_thousand_en(n // value)} {name}")
            n %= value
    if n:
        parts.append(_under_thousand_en(n))
    return " ".join(parts)


def ordinal_to_words_en(n: int) -> str:
    words = number_to_words_en(n).split(" ")
    last = words[-1]
    if "-" in last:
        head, tail = last.rsplit("-", 1)
        words[-1] = f"{head}-{_ORDINAL.get(tail, tail + 'th')}"
    else:
        words[-1] = _ORDINAL.get(last, last + "th")
    return " ".join(words)


def year_to_words_en(year: int) -> str:
    if year == 2000:
        return "two thousand"
    if 2001 <= year <= 2009:
        return f"two thousand {_ONES[year - 2000]}"
    if 2010 <= year <= 2099:
        return f"twenty {_under_thousand_en(year - 2000)}"
    if 1100 <= year <= 1999:
        hi, lo = divmod(year, 100)
        if lo == 0:
            return f"{_ONES[hi]} hundred"
        return f"{_ONES[hi]} {_under_thousand_en(lo)}"
    return number_to_words_en(year)


def decimal_to_words_en(text: str) -> str:
    whole, _, frac = text.partition(".")
    digits = " ".join(_ONES[int(d)] for d in frac if d.isdigit())
    return f"{number_to_words_en(int(whole))} point {digits}"


# ---------------------------------------------------------------------------
# Urdu / Hindi number words (South-Asian grouping: thousand, lakh, crore)
# ---------------------------------------------------------------------------
_UR_0_99 = (
    "صفر ایک دو تین چار پانچ چھ سات آٹھ نو دس گیارہ بارہ تیرہ چودہ پندرہ سولہ "
    "سترہ اٹھارہ انیس بیس اکیس بائیس تئیس چوبیس پچیس چھبیس ستائیس اٹھائیس "
    "انتیس تیس اکتیس بتیس تینتیس چونتیس پینتیس چھتیس سینتیس اڑتیس انتالیس "
    "چالیس اکتالیس بیالیس تینتالیس چوالیس پینتالیس چھیالیس سینتالیس اڑتالیس "
    "انچاس پچاس اکیاون باون تریپن چون پچپن چھپن ستاون اٹھاون انسٹھ ساٹھ "
    "اکسٹھ باسٹھ تریسٹھ چونسٹھ پینسٹھ چھیاسٹھ سڑسٹھ اڑسٹھ انہتر ستر اکہتر "
    "بہتر تہتر چوہتر پچھتر چھہتر ستہتر اٹھہتر اناسی اسی اکیاسی بیاسی تیراسی "
    "چوراسی پچاسی چھیاسی ستاسی اٹھاسی نواسی نوے اکیانوے بانوے تریانوے "
    "چورانوے پچیانوے چھیانوے ستانوے اٹھانوے ننانوے"
).split()

_HI_0_99 = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह "
    "सोलह सत्रह अठारह उन्नीस बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस "
    "अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस "
    "अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चौवालीस पैंतालीस छियालीस "
    "सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन "
    "अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर "
    "सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर "
    "उनासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी "
    "नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे पचानवे छियानवे सत्तानवे "
    "अट्ठानवे निन्यानवे"
).split()


def _sa_number(n: int, table: list[str], hundred: str, thousand: str,
               lakh: str, crore: str) -> str:
    if n == 0:
        return table[0]
    parts: list[str] = []
    if n >= 10_000_000:
        parts.append(_sa_number(n // 10_000_000, table, hundred, thousand, lakh, crore) + f" {crore}")
        n %= 10_000_000
    if n >= 100_000:
        parts.append(f"{table[n // 100_000]} {lakh}")
        n %= 100_000
    if n >= 1_000:
        parts.append(f"{table[n // 1_000]} {thousand}")
        n %= 1_000
    if n >= 100:
        parts.append(f"{table[n // 100]} {hundred}")
        n %= 100
    if n:
        parts.append(table[n])
    return " ".join(parts)


def number_to_words_ur(n: int) -> str:
    return _sa_number(n, _UR_0_99, "سو", "ہزار", "لاکھ", "کروڑ")


def number_to_words_hi(n: int) -> str:
    return _sa_number(n, _HI_0_99, "सौ", "हज़ार", "लाख", "करोड़")


def _number_words(value: str, lang: str) -> str:
    """Spoken form of a plain integer string in the target language."""
    n = int(value)
    # English reads standalone 4-digit years (1100-2099) as years.
    if lang == "en" and len(value) == 4 and 1100 <= n <= 2099:
        return year_to_words_en(n)
    if lang == "ur":
        return number_to_words_ur(n)
    if lang == "hi":
        return number_to_words_hi(n)
    return number_to_words_en(n)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
ABBREVIATIONS = {
    "mr": "Mister", "mrs": "Missus", "ms": "Miss", "dr": "Doctor",
    "st": "Saint", "prof": "Professor", "sr": "Senior", "jr": "Junior",
    "vs": "versus", "etc": "et cetera", "approx": "approximately",
    "dept": "Department", "fig": "Figure", "govt": "government",
    "jan": "January", "feb": "February", "mar": "March", "apr": "April",
    "jun": "June", "jul": "July", "aug": "August", "sep": "September",
    "sept": "September", "oct": "October", "nov": "November", "dec": "December",
}
_CURRENCY = {
    "$": ("dollar", "dollars"), "€": ("euro", "euros"),
    "£": ("pound", "pounds"),
}
_CURRENCY_PREFIX = {"rs": ("rupee", "rupees"), "pkr": ("rupee", "rupees"),
                    "inr": ("rupee", "rupees"), "usd": ("dollar", "dollars")}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]

# Eastern-Arabic (Urdu) and Devanagari digits -> ASCII
_DIGIT_TRANSLATION: dict[int, str] = {}
for _i in range(10):
    _DIGIT_TRANSLATION[0x660 + _i] = str(_i)   # Arabic-Indic
    _DIGIT_TRANSLATION[0x6F0 + _i] = str(_i)   # Extended Arabic-Indic (Urdu)
    _DIGIT_TRANSLATION[0x966 + _i] = str(_i)   # Devanagari


@dataclass
class NormalizedText:
    text: str
    flagged_tokens: list[str] = field(default_factory=list)
    original_text: str = ""


# ---------------------------------------------------------------------------
# Passes
# ---------------------------------------------------------------------------
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\uFE0F\u200D]+")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_STAGE_RE = re.compile(r"\[(?:music|applause|laughter|pause|beat|sound[^\]]*|"
                       r"sfx[^\]]*|intro|outro|scene[^\]]*)\]", re.I)
_PAREN_STAGE_RE = re.compile(
    r"\((?:music|applause|laughter|pause|beat|sfx)[^)]*\)", re.I)


def strip_markup(text: str) -> str:
    text = _URL_RE.sub("", text)
    text = _STAGE_RE.sub("", text)
    text = _PAREN_STAGE_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    # Markdown: headings, emphasis, code, links, list bullets, blockquotes.
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"(\*\*|__|\*|_|`)", "", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    text = re.sub(r"-{3,}", " ", text)
    return text


def _flag_tokens(text: str, lang: str) -> list[str]:
    if lang not in ("ur", "hi"):
        # Still flag ALL-CAPS acronyms in English for dictionary coverage.
        tokens = re.findall(r"\b[A-Z]{2,}\b", text)
    else:
        tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", text)
    seen: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.append(tok)
    return seen


def _normalize_urdu_punct(text: str) -> str:
    # Urdu/Arabic punctuation kept (voices expect it); tidy spacing + ZWNJ.
    text = text.replace("\u200c", "\u200c")  # ZWNJ preserved deliberately
    text = re.sub(r"\s*([،؛۔؟])\s*", r"\1 ", text)
    text = text.replace(" ,", "،").replace(" ?", "؟")
    return text


def normalize_text(text: str, language: str = "en") -> NormalizedText:
    """Full normalization pipeline. Language: 'en' | 'ur' | 'hi' (or locale)."""
    lang = (language or "en").split("-")[0].lower()
    original = text
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_DIGIT_TRANSLATION)
    text = strip_markup(text)
    if lang == "ur":
        text = _normalize_urdu_punct(text)

    num = lambda v: _number_words(str(v), lang)  # noqa: E731

    # Dates: ISO 2026-10-23 and D/M/YYYY-ish slash dates (English spoken form)
    def _iso_date(m: re.Match) -> str:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{_MONTHS[mo - 1]} {ordinal_to_words_en(d)}, {year_to_words_en(y)}"
        return m.group(0)
    text = re.sub(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", _iso_date, text)

    # Times HH:MM
    def _time(m: re.Match) -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        if mi == 0:
            return f"{number_to_words_en(h)} o'clock"
        return f"{number_to_words_en(h)} {number_to_words_en(mi)}"
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", _time, text)

    # Currency prefix: Rs. 500 / PKR 500
    def _cur_prefix(m: re.Match) -> str:
        unit = _CURRENCY_PREFIX[m.group(1).lower()]
        n = float(m.group(2).replace(",", ""))
        word = unit[0] if n == 1 else unit[1]
        spoken = decimal_to_words_en(m.group(2).replace(",", "")) \
            if "." in m.group(2) else num(m.group(2).replace(",", ""))
        return f"{spoken} {word}"
    text = re.sub(r"\b(Rs\.?|PKR|INR|USD)\s*(\d[\d,]*(?:\.\d+)?)", _cur_prefix,
                  text, flags=re.I)

    # Currency symbols: $500
    def _cur_sym(m: re.Match) -> str:
        unit = _CURRENCY[m.group(1)]
        raw = m.group(2).replace(",", "")
        n = float(raw)
        word = unit[0] if n == 1 else unit[1]
        spoken = decimal_to_words_en(raw) if "." in raw else num(raw)
        return f"{spoken} {word}"
    text = re.sub(r"([$€£])\s*(\d[\d,]*(?:\.\d+)?)", _cur_sym, text)

    # Percent: 50%
    def _pct(m: re.Match) -> str:
        raw = m.group(1).replace(",", "")
        spoken = decimal_to_words_en(raw) if "." in raw else num(raw)
        return f"{spoken} percent"
    text = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s*%", _pct, text)

    # Decimals
    text = re.sub(r"\b\d+\.\d+\b",
                  lambda m: decimal_to_words_en(m.group(0))
                  if lang == "en" else m.group(0), text)

    # Integers (with thousands separators), not glued to letters.
    text = re.sub(r"(?<![\w])\d{1,3}(?:,\d{3})+(?![\w])",
                  lambda m: num(m.group(0).replace(",", "")), text)
    text = re.sub(r"(?<![\w.])\d+(?![\w.])",
                  lambda m: num(m.group(0)), text)

    # Abbreviations (word followed by '.'), English.
    if lang == "en":
        def _abbr(m: re.Match) -> str:
            return ABBREVIATIONS.get(m.group(1).lower(), m.group(0))
        text = re.sub(r"\b([A-Za-z]{2,})\.(\s|$)", lambda m: _abbr(m) + (m.group(2) or ""), text)
        text = text.replace("e.g.", "for example").replace("i.e.", "that is")

    # Symbols
    text = text.replace("&", " and " if lang == "en" else " اور " if lang == "ur" else " और ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    flagged = _flag_tokens(text, lang)
    return NormalizedText(text=text, flagged_tokens=flagged, original_text=original)
