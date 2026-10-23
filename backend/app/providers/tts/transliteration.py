"""Pre-TTS transliteration — Roman Urdu -> Urdu script (Round 4, Worker 3).

WHY THIS EXISTS
---------------
The roadmap's voice-workflow rule: Roman Urdu narration should be
transliterated to Urdu script BEFORE synthesis, because ur-PK voices
misread Latin-script Urdu.  It also fixes a measurement artefact:
Whisper emits Urdu script, so subtitle script-match against a Roman
script is meaningless (~0.0 on every Round 3 Phase-0 video).

DESIGN — deterministic, offline, no LLM/API, pure functions
------------------------------------------------------------
``to_urdu_script(text)`` applies, in order:

1. **Pronunciation dictionary FIRST** (``dictionary.py``, the bundled
   global + per-niche YAML layers, merged with any caller-supplied
   :class:`PronunciationDictionary`; caller entries win).  The
   dictionary's policy is followed *exactly*, never a guessed
   transliteration:

   * Latin surface with an ``alias`` (e.g. ``NASA`` -> ``"Nassa"``,
     ``YouTube`` -> ``"You Tube"``, ``AI`` -> ``"A I"``,
     ``paracetamol`` -> ``"para seat a moll"``): the token is replaced
     by the dictionary's respelled alias, verbatim.  These stay Latin
     on purpose — the dictionary says respell, not transliterate.
   * Urdu-script surface with a Latin ``alias`` (e.g. surface
     ``کرکٹ`` / alias ``"cricket"``, surface ``حضرت یوسف`` / alias
     ``"Hazrat Yousuf"``): the alias IS the dictionary's Latin name for
     a word whose naturalised Urdu spelling is the surface, so Latin
     occurrences of the alias are replaced by the Urdu surface.
     Multi-word aliases are matched as phrases, longest first.

2. **Curated lexicon** of high-frequency Roman Urdu words (function
   words, common verbs/nouns) whose spelling the rules below cannot
   derive unambiguously (``mein``->میں, ``khabar``->خبر vs
   ``kahani``->کہانی, retroflex/dental and ح/ہ/ھ distinctions, ...).

3. **Rule engine** for the remaining Roman Urdu words: greedy
   longest-match scan with aspirate digraphs (kh/gh/ch/chh/sh/zh/th/
   bh/ph/dh/jh/rh), long vowels (aa->ا, ee/ii->ی, oo/uu->و), short
   vowels normally unwritten (Urdu orthography drops zabar/zer/pesh),
   word-final conventions (final ``a``->ا, ``e``->ے, ``i``->ی,
   ``o``->و, final ``-ah``->ہ, trailing ``h`` after a long vowel
   dropped), and doubled consonants collapsed.

4. **Unknown English passthrough (deliberate, documented choice):**
   a Latin token that is neither in the dictionary, nor the lexicon,
   nor plausibly Roman Urdu (it is in the curated English list, or
   carries an English-only pattern such as ``-tion``/``-sion``/
   ``-ing``, ``sci-``, a silent-e shape, or has no vowel at all) is
   left in Latin script UNCHANGED and counted/logged.  ur-PK voices
   handle isolated English words better than a guessed
   transliteration, and the count is surfaced to callers
   (:class:`TransliterationResult.unknown_tokens`) so the A/B harness
   (Worker 4) and the production report can see exactly how much of a
   script stayed Latin.

INVARIANTS
----------
* Pure + deterministic: same input + same dictionary -> same output.
* Idempotent: Urdu-script spans (any token containing a character in
  U+0600..U+06FF) pass through unchanged, so running the function on
  its own output is a no-op.
* Western digits are kept as-is (TTS voices read them; the A/B worker
  measures this).  Punctuation and whitespace are preserved.

COVERAGE HONESTY — what this does BADLY (read before trusting it)
-----------------------------------------------------------------
* Roman Urdu does not mark dental vs retroflex (t/d), ح vs ہ, س vs ص
  vs ث, ز vs ذ vs ض vs ظ, or short-vowel length.  The rule engine
  picks the common dental/plain letter; only lexicon/dictionary words
  get the marked forms.  Unseen words WILL sometimes get a readable
  but non-standard spelling.
* ``a`` length is a heuristic (کہانی/خبر ambiguity is real Urdu
  romanisation ambiguity, not fixable deterministically).
* Nasalisation (ں) is only produced by lexicon words (میں، ہیں،
  نہیں، کیوں ...); rule words ending in ``n`` get ن.
* English detection is a heuristic list + patterns: an uncommon
  English word that looks like Roman Urdu will be transliterated,
  and an uncommon Roman Urdu word that looks English (e.g. ending
  in ``-ing``) will be passed through.  Both are counted nowhere —
  the unknown-token count only covers the passthrough decisions.
* Names not in the lexicon/dictionary are rule-transliterated
  (e.g. ``Imran`` -> امران — the lexicon carries common Pakistani
  names, but the long tail is approximate).

This module has NO import side effects: the bundled dictionary YAML
is loaded lazily on first use and cached.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache

__all__ = [
    "TransliterationResult",
    "to_urdu_script",
    "transliterate",
    "transliterate_with_stats",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Curated Roman Urdu lexicon (lowercase roman -> Urdu script).
# Only words whose spelling the rules cannot derive reliably, plus the
# highest-frequency function words (pinned so they can never regress).
# ---------------------------------------------------------------------------
_LEXICON: dict[str, str] = {
    # function words / pronouns / auxiliaries
    "ka": "کا", "ki": "کی", "ke": "کے", "ko": "کو", "ne": "نے",
    "se": "سے", "mein": "میں", "mai": "میں", "main": "میں",
    "me": "میں", "par": "پر", "per": "پر", "tak": "تک", "bhi": "بھی",
    "hi": "ہی", "toh": "تو", "to": "تو", "aur": "اور", "ya": "یا",
    "hai": "ہے", "hain": "ہیں", "tha": "تھا", "thi": "تھی",
    "thay": "تھے", "the": "تھے", "ho": "ہو", "hoga": "ہوگا",
    "hogi": "ہوگی", "honge": "ہوں گے", "hua": "ہوا", "hui": "ہوئی",
    "hue": "ہوئے", "hota": "ہوتا", "hoti": "ہوتی", "hote": "ہوتے",
    "yeh": "یہ", "ye": "یہ", "woh": "وہ", "wo": "وہ", "is": "اس",
    "us": "اس", "in": "ان", "un": "ان", "kis": "کس", "jis": "جس",
    "kya": "کیا", "kyun": "کیوں", "kyon": "کیوں", "kyunke": "کیونکہ",
    "kyunki": "کیونکہ", "kab": "کب", "jab": "جب", "tab": "تب",
    "ab": "اب", "phir": "پھر", "lekin": "لیکن", "magar": "مگر",
    "agar": "اگر", "kaisa": "کیسا", "kaisi": "کیسی", "aisa": "ایسا",
    "aisi": "ایسی", "jaisa": "جیسا", "waisa": "ویسا", "itna": "اتنا",
    "utna": "اتنا", "jitna": "جتنا", "kitna": "کتنا", "kitni": "کتنی",
    "itni": "اتنی", "apna": "اپنا", "apni": "اپنی", "apne": "اپنے",
    "mera": "میرا", "meri": "میری", "mere": "میرے", "tera": "تیرا",
    "teri": "تیری", "uska": "اس کا", "uski": "اس کی", "iska": "اس کا",
    "iski": "اس کی", "hum": "ہم", "tum": "تم", "aap": "آپ",
    "humne": "ہم نے", "tumne": "تم نے", "usne": "اس نے", "isne": "اس نے",
    "kisne": "کس نے", "sab": "سب", "kuch": "کچھ", "koi": "کوئی",
    "ko": "کو", "nahi": "نہیں", "nahin": "نہیں", "na": "نہ",
    "mat": "مت", "zaroor": "ضرور", "shayad": "شاید", "bhi": "بھی",
    "sirf": "صرف", "bina": "بنا", "baad": "بعد", "pehle": "پہلے",
    "pehlay": "پہلے", "sath": "ساتھ", "saath": "ساتھ", "liye": "لیے",
    "taraf": "طرف", "andar": "اندر", "bahar": "باہر", "upar": "اوپر",
    "neeche": "نیچے", "neechay": "نیچے", "aage": "آگے", "peeche": "پیچھے",
    "yahan": "یہاں", "wahan": "وہاں", "kahan": "کہاں", "idhar": "ادھر",
    "udhar": "ادھر", "kidhar": "کدھر", "jabke": "جبکہ", "halanke": "حالانکہ",
    "warna": "ورنہ", "isliye": "اس لیے", "isiliye": "اسی لیے",
    "bilkul": "بالکل",
    # common verbs (dictionary forms / frequent inflections)
    "karna": "کرنا", "karta": "کرتا", "karti": "کرتی", "karte": "کرتے",
    "kiya": "کیا", "kiye": "کیے", "kar": "کر", "karke": "کر کے",
    "raha": "رہا", "rahi": "رہی", "rahe": "رہے", "rehna": "رہنا",
    "gaya": "گیا", "gayi": "گئی", "gaye": "گئے", "jaana": "جانا",
    "jata": "جاتا", "jati": "جاتی", "aaya": "آیا", "aayi": "آئی",
    "aaye": "آئے", "aana": "آنا", "aata": "آتا", "aati": "آتی",
    "diya": "دیا", "diye": "دیے", "dena": "دینا", "deta": "دیتا",
    "liya": "لیا", "liye": "لیے", "lena": "لینا", "leta": "لیتا",
    "bhejo": "بھیجو", "bheja": "بھیجا", "bhejna": "بھیجنا",
    "dekha": "دیکھا", "dekho": "دیکھو", "dekhna": "دیکھنا",
    "dekhta": "دیکھتا", "suna": "سنا", "sunna": "سننا", "kaha": "کہا",
    "kehna": "کہنا", "kehta": "کہتا", "bola": "بولا", "bolna": "بولنا",
    "huaa": "ہوا", "jeeta": "جیتا", "jeet": "جیت", "haara": "ہارا",
    "haar": "ہار", "khela": "کھیلا", "khel": "کھیل", "mila": "ملا",
    "milna": "ملنا", "bana": "بنا", "banana": "بنانا", "banaya": "بنایا",
    "samjha": "سمجھا", "samajh": "سمجھ", "socha": "سوچا", "soch": "سوچ",
    "chala": "چلا", "chalna": "چلنا", "ruka": "رکا", "khara": "کھڑا",
    "khari": "کھڑی", "betha": "بیٹھا", "baitha": "بیٹھا",
    "utha": "اٹھا", "uthna": "اٹھنا", "soya": "سویا", "jaaga": "جاگا",
    "khana": "کھانا", "khaya": "کھایا", "peena": "پینا", "piya": "پیا",
    "likha": "لکھا", "likhna": "لکھنا", "parha": "پڑھا",
    "parhna": "پڑھنا", "seekha": "سیکھا", "seekhna": "سیکھنا",
    "chahiye": "چاہیے", "chahte": "چاہتے", "sakta": "سکتا",
    "sakti": "سکتی", "sakte": "سکتے", "sakay": "سکے",
    "lagta": "لگتا", "lagti": "لگتی", "laga": "لگا", "lagi": "لگی",
    "rehta": "رہتا", "rehti": "رہتی", "rehte": "رہتے",
    "milta": "ملتا", "milti": "ملتی", "milte": "ملتے",
    "hazaar": "ہزار", "shuru": "شروع", "khatam": "ختم",
    "ibteda": "ابتدا", "aaghaz": "آغاز",
    # common nouns / adjectives / time words
    "kahani": "کہانی", "kahaniyan": "کہانیاں", "kahaniyon": "کہانیوں",
    "khabar": "خبر", "baat": "بات", "baatein": "باتیں", "baaton": "باتوں",
    "baatay": "باتیں", "waqt": "وقت", "saal": "سال", "din": "دن",
    "raat": "رات", "subah": "صبح", "shaam": "شام", "dopahar": "دوپہر",
    "aaj": "آج", "kal": "کل", "parson": "پرسوں", "hafta": "ہفتہ",
    "mahina": "مہینہ", "mahine": "مہینے", "lamha": "لمحہ",
    "lamhe": "لمحے", "dafa": "دفعہ", "baar": "بار", "duniya": "دنیا",
    "zindagi": "زندگی", "mohabbat": "محبت", "nafrat": "نفرت",
    "khushi": "خوشی", "gham": "غم", "dard": "درد", "dil": "دل",
    "dimagh": "دماغ", "aankh": "آنکھ", "aankhein": "آنکھیں",
    "haath": "ہاتھ", "sar": "سر", "chehra": "چہرہ", "awaaz": "آواز",
    "log": "لوگ", "logon": " لوگوں".strip(), "aadmi": "آدمی",
    "aurat": "عورت", "larka": "لڑکا", "larki": "لڑکی", "bacha": "بچہ",
    "bachay": "بچے", "dost": "دوست", "doston": "دوستوں",
    "ghar": "گھر", "sheher": "شہر", "shehar": "شہر", "mulk": "ملک",
    "gaon": "گاؤں", "mohalla": "محلہ", "galiyan": "گلیاں",
    "jagah": "جگہ", "tarah": "طرح", "tarha": "طرح", "wala": "والا",
    "wali": "والی", "wale": "والے", "walon": "والوں",
    "samundar": "سمندر", "gehrai": "گہرائی", "gehraiyon": "گہرائیوں",
    "raaz": "راز", "pahaar": "پہاڑ", "pahar": "پہاڑ", "darya": "دریا",
    "nadi": "ندی", "jangal": "جنگل", "registan": "ریگستان",
    "aasman": "آسمان", "zameen": "زمین", "suraj": "سورج",
    "chaand": "چاند", "chand": "چاند", "sabar": "صبر",
    "sitaare": "ستارے", "sitara": "ستارہ",
    "hawa": "ہوا", "baarish": "بارش", "barish": "بارش",
    "mausam": "موسم", "sardi": "سردی", "garmi": "گرمی",
    "khaana": "کھانا", "paani": "پانی", "paisa": "پیسہ",
    "paise": "پیسے", "kaam": "کام", "naam": "نام", "ilam": "علم",
    "ilm": "علم", "taleem": "تعلیم", "ustaad": "استاد",
    "shagird": "شاگرد", "kitab": "کتاب", "kitabein": "کتابیں",
    "qalam": "قلم", "kaghaz": "کاغذ", "safha": "صفحہ",
    "lafz": "لفظ", "jumla": "جملہ", "sawaal": "سوال",
    "jawab": "جواب", "sawal": "سوال", "haqeeqat": "حقیقت",
    "haqiqat": "حقیقت", "jhoot": "جھوٹ", "sach": "سچ",
    "sachai": "سچائی", "ghalti": "غلطی", "ghalat": "غلط",
    "theek": "ٹھیک", "acha": "اچھا", "achha": "اچھا",
    "achi": "اچھی", "achhi": "اچھی", "bura": "برا", "buri": "بری",
    "bara": "بڑا", "bari": "بڑی", "bare": "بڑے", "chota": "چھوٹا",
    "chhota": "چھوٹا", "choti": "چھوٹی", "naya": "نیا", "nayi": "نئی",
    "purana": "پرانا", "purani": "پرانی", "lamba": "لمبا",
    "gehra": "گہرا", "gehri": "گہری", "khoobsurat": "خوبصورت",
    "khubsurat": "خوبصورت", "ajeeb": "عجیب", "mushkil": "مشکل",
    "aasaan": "آسان", "asan": "آسان", "khaas": "خاص", "aam": "عام",
    "har": "ہر", "pehla": "پہلا", "pehli": "پہلی", "pehle": "پہلے",
    "dusra": "دوسرا", "doosra": "دوسرا", "dusri": "دوسری",
    "teesra": "تیسرا", "aakhri": "آخری", "akhri": "آخری",
    "aakhri": "آخری", "agla": "اگلا", "agli": "اگلی",
    "pichla": "پچھلا", "bahut": "بہت", "bohat": "بہت", "bohut": "بہت",
    "zyada": "زیادہ", "ziada": "زیادہ", "kam": "کم", "thoda": "تھوڑا",
    "thodi": "تھوڑی", "thora": "تھوڑا", "pura": "پورا",
    "puri": "پوری", "aadha": "آدھا", "aadhi": "آدھی",
    "do": "دو", "teen": "تین", "chaar": "چار", "paanch": "پانچ",
    "cheh": "چھ", "chhe": "چھ", "saat": "سات", "aath": "آٹھ",
    "nau": "نو", "das": "دس", "ek": "ایک", "hazaar": "ہزار",
    "lakh": "لاکھ", "crore": "کروڑ", "pehle": "پہلے",
    "tayyar": "تیار", "taiyar": "تیار", "tayyari": "تیاری",
    "dilchasp": "دلچسپ", "dilchaspi": "دلچسپی", "hairan": "حیران",
    "hairat": "حیرت", "khauf": "خوف", "dar": "ڈر", "umeed": "امید",
    "ummid": "امید", "umeedain": "امیدیں", "yaqeen": "یقین",
    "shak": "شک", "gussa": "غصہ", "khamosh": "خاموش",
    "khamoshi": "خاموشی", "shor": "شور", "roz": "روز",
    "rozana": "روزانہ", "aksar": "اکثر", "hamesha": "ہمیشہ",
    "kabhi": "کبھی", "shayed": "شاید", "zaroor": "ضرور",
    "istemal": "استعمال", "istemaal": "استعمال", "matlab": "مطلب",
    "mauka": "موقع", "moqa": "موقع", "saboot": "ثبوت",
    "sabut": "ثبوت", "insaaf": "انصاف", "insaf": "انصاف",
    "adalat": "عدالت", "qanoon": "قانون", "qanun": "قانون",
    "mujrim": "مجرم", "jurm": "جرم", "saza": "سزا",
    "police": "پولیس", "fauj": "فوج", "sipahi": "سپاہی",
    "badshah": "بادشاہ", "malika": "ملکہ", "shehzada": "شہزادہ",
    "wazir": "وزیر", "mahal": "محل", "qila": "قلعہ",
    "jang": "جنگ", "fatah": "فتح", "shikast": "شکست",
    "tareekh": "تاریخ", "tarikh": "تاریخ", "qadeem": "قدیم",
    "sadi": "صدی", "dour": "دور", "daur": "دور", "zamana": "زمانہ",
    "zamaana": "زمانہ", "zamanay": "زمانے", "waqia": "واقعہ",
    "waqiya": "واقعہ", "waqiat": "واقعات", "haal": "حال",
    "haalat": "حالات", "haalaat": "حالات", "khabrein": "خبریں",
    "allah": "اللہ", "khuda": "خدا", "islam": "اسلام",
    "muslim": "مسلم", "musulman": "مسلمان", "imaan": "ایمان",
    "iman": "ایمان", "ibadat": "عبادت", "dua": "دعا",
    "masjid": "مسجد", "namaz": "نماز", "roza": "روزہ",
    "quran": "قرآن", "hadees": "حدیث", "hadith": "حدیث",
    "nabi": "نبی", "rasool": "رسول", "hazrat": "حضرت",
    "sehat": "صحت", "bemari": "بیماری", "bimari": "بیماری",
    "dawai": "دوائی", "doctor": "ڈاکٹر", "mareez": "مریض",
    "mariz": "مریض", "hospital": "ہسپتال", "aspataal": "ہسپتال",
    "zehan": "ذہن", "zehn": "ذہن", "jism": "جسم", "khoon": "خون",
    "cancer": "کینسر", "bukhaar": "بخار", "bukhar": "بخار",
    # places / names (common in narration)
    "pakistan": "پاکستان", "pakistani": "پاکستانی", "india": "انڈیا",
    "bharat": "بھارت", "lahore": "لاہور", "karachi": "کراچی",
    "islamabad": "اسلام آباد", "peshawar": "پشاور", "quetta": "کوئٹہ",
    "multan": "ملتان", "faisalabad": "فیصل آباد", "sindh": "سندھ",
    "punjab": "پنجاب", "balochistan": "بلوچستان", "kashmir": "کشمیر",
    "imran": "عمران", "ahmed": "احمد", "ahmad": "احمد", "ali": "علی",
    "muhammad": "محمد", "mohammad": "محمد", "hassan": "حسن",
    "hussain": "حسین", "husain": "حسین", "fatima": "فاطمہ",
    "aisha": "عائشہ", "ayesha": "عائشہ", "zainab": "زینب",
    "umar": "عمر", "usman": "عثمان", "abu": "ابو", "ibn": "ابن",
    "khan": "خان", "babar": "بابر", "azam": "اعظم", "akbar": "اکبر",
    "iqbal": "اقبال", "jinnah": "جناح", "quaid": "قائد",
    "quaideazam": "قائد اعظم", "mohenjodaro": "موہنجوداڑو",
    "mohenjo": "موہنجوداڑو", "harappa": "ہڑپہ", "england": "انگلینڈ",
    "australia": "آسٹریلیا", "melbourne": "میلبورن",
}

# ---------------------------------------------------------------------------
# English passthrough: curated common English words + patterns.
# Deliberate choice (see module docstring): these stay Latin + counted.
# NOTE: words that are also Roman Urdu particles (to, me, is, us, in,
# par, do, teen...) are intentionally ABSENT — the lexicon wins.
# ---------------------------------------------------------------------------
_ENGLISH_WORDS: set[str] = {
    "the", "and", "of", "was", "were", "are", "with", "from", "this",
    "that", "have", "has", "had", "will", "would", "can", "could",
    "should", "about", "which", "their", "there", "here", "what",
    "when", "where", "who", "how", "why", "all", "one", "two",
    "first", "second", "world", "cup", "final", "match", "matches",
    "team", "teams", "player", "players", "stadium", "tournament",
    "champion", "champions", "trophy", "score", "scores", "century",
    "wicket", "wickets", "batting", "bowling", "batsman", "bowler",
    "captain", "coach", "umpire", "over", "overs", "run", "runs",
    "ball", "balls", "bat", "boundary", "sixer", "video", "videos",
    "channel", "subscribe", "subscribed", "like", "share", "comment",
    "comments", "please", "watch", "watching", "history", "story",
    "stories", "fact", "facts", "science", "scientist", "scientists",
    "ocean", "deep", "sea", "water", "earth", "planet", "space",
    "moon", "sun", "star", "stars", "light", "year", "years",
    "day", "days", "night", "king", "queen", "war", "city",
    "people", "man", "woman", "men", "women", "child", "children",
    "new", "old", "big", "great", "good", "best", "last", "next",
    "part", "life", "time", "way", "end", "point", "level",
    "system", "data", "internet", "computer", "mobile", "phone",
    "online", "website", "google", "facebook", "twitter", "message",
    "instagram", "technology", "digital", "electronic", "machine",
    "power", "energy", "health", "body", "brain", "heart",
    "blood", "disease", "treatment", "medicine", "virus",
    "bacteria", "climate", "weather", "animal", "animals",
    "human", "humans", "country", "countries", "government",
    "president", "minister", "election", "party", "group",
    "company", "market", "money", "price", "business",
    "school", "college", "university", "student", "students",
    "teacher", "book", "books", "language", "english", "urdu",
}

_ENGLISH_PATTERNS = (
    re.compile(r"(tion|sion|cious|tious|eous|ture|ough|eigh)$"),
    re.compile(r"^(sci|psych|kn|wr|gn)"),
    re.compile(r"(ing|ness|ment|ity|ies)$"),
    re.compile(r"^[bcdfghjklmnpqrstvwxz]+$"),  # no vowel at all
)

# Silent-e English shape (consonant + vowel + consonant + final e),
# applied only when the word is not lexicon/dictionary Roman Urdu.
_SILENT_E = re.compile(r"^[bcdfghjklmnpqrstvwxyz]+[aeiou][bcdfghjklmnpqrstvwxyz]e$")


def _looks_english(word: str) -> bool:
    if word in _ENGLISH_WORDS:
        return True
    if any(p.search(word) for p in _ENGLISH_PATTERNS):
        # ``-ing`` etc. need a minimum stem so short Urdu words survive.
        return len(word) > 4 or word in _ENGLISH_WORDS
    if _SILENT_E.match(word) and len(word) >= 5:
        return True
    return False


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------
_DIGRAPHS: dict[str, str] = {
    "chh": "چھ",
    "kh": "خ", "gh": "غ", "ch": "چ", "sh": "ش", "zh": "ژ",
    "th": "تھ", "bh": "بھ", "ph": "پھ", "dh": "دھ", "jh": "جھ",
    "rh": "ڑھ",
}
_CONSONANTS: dict[str, str] = {
    "b": "ب", "c": "ک", "d": "د", "f": "ف", "g": "گ", "h": "ہ",
    "j": "ج", "k": "ک", "l": "ل", "m": "م", "n": "ن", "p": "پ",
    "q": "ق", "r": "ر", "s": "س", "t": "ت", "v": "و", "w": "و",
    "x": "کس", "y": "ی", "z": "ز",
}
_VOWELS = set("aeiou")


def _transliterate_word_rules(word: str) -> str:
    """Rule-transliterate ONE lowercase Roman Urdu word.

    Heuristics (see module docstring for the honesty notes): short
    vowels are unwritten, long vowels are written, final vowels use
    the word-final conventions, doubled consonants collapse.
    """
    out: list[str] = []
    i, n = 0, len(word)
    while i < n:
        ch = word[i]
        rest = word[i:]

        # -- word-final conventions ------------------------------------
        if i > 0 and rest in ("ah",) and len(rest) == 2:
            out.append("ہ")          # final -ah -> ہ (wajah -> وجہ)
            break
        if i > 0 and ch == "h" and i == n - 1:
            break                    # trailing h after a vowel: dropped

        # -- initial vowel (needs an alif carrier) ----------------------
        if i == 0 and ch in _VOWELS:
            if rest.startswith(("aa", "ai", "ay")):
                out.append("آ" if rest.startswith("aa") else "اے")
                i += 2
            elif rest.startswith(("au", "oo", "uu")):
                out.append("او")
                i += 2
            elif rest.startswith(("ee", "ii")):
                out.append("ای")
                i += 2
            elif ch == "o":
                out.append("او")
                i += 1
            elif ch == "e":
                out.append("اے")
                i += 1
            else:  # a / i / u
                out.append("ا")
                i += 1
            continue

        # -- aspirate digraphs / trigraph (longest match) ---------------
        matched = False
        for size in (3, 2):
            seq = word[i:i + size]
            if seq in _DIGRAPHS:
                out.append(_DIGRAPHS[seq])
                i += size
                matched = True
                break
        if matched:
            continue

        # -- doubled consonant collapses --------------------------------
        if ch not in _VOWELS and i + 1 < n and word[i + 1] == ch:
            out.append(_CONSONANTS.get(ch, ch))
            i += 2
            continue

        # -- vowels ------------------------------------------------------
        if ch in _VOWELS:
            nxt = word[i + 1] if i + 1 < n else ""
            is_final = i == n - 1
            pair = word[i:i + 2]
            if pair in ("aa",):
                out.append("ا")
                i += 2
            elif pair in ("ee", "ii"):
                out.append("ی")
                i += 2
            elif pair in ("oo", "uu", "au"):
                out.append("و")
                i += 2
            elif pair == "ai":
                out.append("ئی" if is_final or i + 2 >= n else "ئ")
                i += 2
            elif pair == "ay":
                out.append("ے" if i + 2 >= n else "ی")
                i += 2
            elif ch == "a":
                if is_final:
                    out.append("ا")
                elif nxt and nxt not in _VOWELS and i + 2 >= n:
                    out.append("ا")  # a + final consonant: long (kitab)
                # else: short zabar — unwritten
                i += 1
            elif ch == "e":
                if is_final:
                    out.append("ے")
                # else: short zer — unwritten
                i += 1
            elif ch == "i":
                if is_final:
                    out.append("ی")
                i += 1
            elif ch == "o":
                out.append("و")
                i += 1
            elif ch == "u":
                if is_final:
                    out.append("و")
                # else: short pesh — unwritten
                i += 1
            else:  # pragma: no cover - exhaustive above
                i += 1
            continue

        # -- plain consonant ---------------------------------------------
        out.append(_CONSONANTS.get(ch, ch))
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Dictionary integration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _DictMaps:
    #: latin token (lower) -> replacement exactly as dictionary specifies
    single: dict[str, str]
    #: latin phrase (lower) -> replacement, longest first
    phrases: tuple[tuple[str, str], ...]
    #: dictionary alias outputs (respellings like "You Tube" / "A I"):
    #: already-dictionary-processed Latin, protected verbatim so the
    #: function stays idempotent on its own output.
    verbatim_single: dict[str, str] = field(default_factory=dict)
    verbatim_phrases: tuple[tuple[str, str], ...] = ()


_EMPTY_MAPS = _DictMaps(single={}, phrases=())


def _maps_from_entries(entries) -> _DictMaps:
    single: dict[str, str] = {}
    phrases: dict[str, str] = {}
    verbatim_single: dict[str, str] = {}
    verbatim_phrases: dict[str, str] = {}
    for entry in entries or []:
        surface = str(getattr(entry, "surface", "") or "").strip()
        alias = str(getattr(entry, "alias", "") or "").strip()
        if not surface:
            continue
        surface_is_urdu = any("\u0600" <= c <= "\u06ff" for c in surface)
        surface_is_latin = bool(re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", surface))
        if surface_is_latin:
            # Policy: respelled alias exactly as specified (or the
            # surface itself when the entry carries no alias).
            single[surface.lower()] = alias if alias else surface
            if alias and alias.lower() != surface.lower() and re.fullmatch(
                    r"[A-Za-z][A-Za-z .'-]*", alias):
                # The alias output itself is finished text: protect it
                # verbatim on any later pass (idempotence).
                key = re.sub(r"\s+", " ", alias.lower()).strip(" .")
                if " " in key:
                    verbatim_phrases[key] = alias
                elif key:
                    verbatim_single[key] = alias
        elif surface_is_urdu and alias and re.fullmatch(
                r"[A-Za-z][A-Za-z .'-]*", alias):
            # Urdu surface named in Latin by its alias: the alias's
            # naturalised Urdu spelling IS the surface.
            key = re.sub(r"\s+", " ", alias.lower()).strip(" .")
            if " " in key:
                phrases[key] = surface
            elif key:
                single[key] = surface
    ordered = tuple(sorted(phrases.items(), key=lambda kv: -len(kv[0])))
    verbatim_ordered = tuple(sorted(
        verbatim_phrases.items(), key=lambda kv: -len(kv[0])))
    return _DictMaps(single=single, phrases=ordered,
                     verbatim_single=verbatim_single,
                     verbatim_phrases=verbatim_ordered)


@lru_cache(maxsize=8)
def _bundled_maps(niche: str | None) -> _DictMaps:
    try:
        from .dictionary import PronunciationDictionary

        return _maps_from_entries(
            PronunciationDictionary.load_bundled(niche=niche).entries)
    except Exception:  # dictionary files are optional; rules still work
        logger.debug("transliteration: bundled dictionary unavailable",
                     exc_info=True)
        return _EMPTY_MAPS


def _resolve_maps(dictionary=None, niche: str | None = None) -> _DictMaps:
    """Bundled maps merged with a caller dictionary (caller wins)."""
    base = _bundled_maps(niche)
    if dictionary is None:
        return base
    entries = getattr(dictionary, "entries", dictionary)
    extra = _maps_from_entries(entries)
    single = {**base.single, **extra.single}
    phrases = dict(base.phrases)
    phrases.update(dict(extra.phrases))
    ordered = tuple(sorted(phrases.items(), key=lambda kv: -len(kv[0])))
    verbatim_single = {**base.verbatim_single, **extra.verbatim_single}
    verbatim_phrases = dict(base.verbatim_phrases)
    verbatim_phrases.update(dict(extra.verbatim_phrases))
    verbatim_ordered = tuple(sorted(
        verbatim_phrases.items(), key=lambda kv: -len(kv[0])))
    return _DictMaps(single=single, phrases=ordered,
                     verbatim_single=verbatim_single,
                     verbatim_phrases=verbatim_ordered)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
@dataclass
class TransliterationResult:
    text: str
    #: Latin tokens left unchanged as unknown English, in order
    #: (occurrences, not uniques — the count is the honest workload).
    unknown_tokens: list[str] = field(default_factory=list)
    #: Dictionary-covered tokens/phrases replaced, in order.
    dictionary_hits: list[str] = field(default_factory=list)
    #: Tokens transliterated via lexicon or rules.
    transliterated_tokens: int = 0

    @property
    def unknown_token_count(self) -> int:
        return len(self.unknown_tokens)


_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_PLACEHOLDER_RE = re.compile("\ue000(\\d+)\ue001")


def transliterate(text: str, *, dictionary=None,
                    niche: str | None = None) -> TransliterationResult:
    """Roman Urdu -> Urdu script with full provenance stats.

    ``dictionary`` may be a ``PronunciationDictionary``, a list of
    ``PronunciationEntry``-shaped objects, or ``None`` (bundled
    global + ``niche`` layers).  Pure and deterministic.
    """
    if not text:
        return TransliterationResult(text=text or "")
    maps = _resolve_maps(dictionary, niche)
    result = TransliterationResult(text=text)

    working = text
    protected: list[str] = []

    def _protect(pattern_text: str, replacement_fn) -> None:
        nonlocal working
        pattern = re.compile(
            r"(?<![A-Za-z])" + re.escape(pattern_text) + r"(?![A-Za-z])",
            re.I)
        working = pattern.sub(replacement_fn, working)

    # 0. Dictionary alias outputs (respellings) are finished text:
    # protect them verbatim FIRST so a re-run is a no-op.
    def _verbatim_sub(m):
        protected.append(m.group(0))
        result.dictionary_hits.append(m.group(0))
        return f"\ue000{len(protected) - 1}\ue001"

    for phrase, _alias in maps.verbatim_phrases:
        _protect(phrase, _verbatim_sub)

    # 1. Dictionary phrases (longest first), protected from reprocessing.
    for phrase, replacement in maps.phrases:
        def _sub(m, replacement=replacement):
            protected.append(replacement)
            result.dictionary_hits.append(m.group(0))
            return f"\ue000{len(protected) - 1}\ue001"

        _protect(phrase, _sub)

    # 2-4. Per-token: dictionary -> lexicon -> English -> rules.
    def _token(m: re.Match) -> str:
        token = m.group(0)
        key = token.lower().strip("'")
        if key in maps.verbatim_single:
            result.dictionary_hits.append(token)
            return token
        if key in maps.single:
            result.dictionary_hits.append(token)
            return maps.single[key]
        if key in _LEXICON:
            result.transliterated_tokens += 1
            return _LEXICON[key]
        if _looks_english(key):
            result.unknown_tokens.append(token)
            return token
        result.transliterated_tokens += 1
        return _transliterate_word_rules(key)

    working = _WORD_RE.sub(_token, working)

    # Restore protected dictionary-phrase replacements.
    working = _PLACEHOLDER_RE.sub(
        lambda m: protected[int(m.group(1))], working)
    result.text = working
    if result.unknown_tokens:
        logger.info(
            "transliteration: %d unknown English token(s) left in Latin "
            "script: %s", len(result.unknown_tokens),
            ", ".join(sorted(set(result.unknown_tokens))))
    return result


def transliterate_with_stats(text: str, **kwargs) -> TransliterationResult:
    """Alias of :func:`transliterate` (name kept for call-site clarity)."""
    return transliterate(text, **kwargs)


def to_urdu_script(text: str, *, dictionary=None,
                   niche: str | None = None) -> str:
    """Roman Urdu -> Urdu script.  Stable contract for Worker 4's A/B
    harness: ``to_urdu_script(text) -> str``, importable with no side
    effects.  Already-Urdu input is returned unchanged; Western digits
    and punctuation are preserved; unknown English words stay Latin
    (see module docstring)."""
    return transliterate(text, dictionary=dictionary, niche=niche).text
