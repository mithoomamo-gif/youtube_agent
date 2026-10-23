"""Scene -> stock-search query ladder (Round 3, roadmap section 24).

Pure, deterministic, no I/O: given ONE scene and its niche profile,
:func:`build_query_ladder` returns 1-3 English search queries, most
specific first, following the section-24 per-scene ladder:

1. **Exact visual query** -- concrete nouns + setting + action extracted
   from the scene's visual description / narration, in English (both
   free catalogues, Pixabay/Pexels, are English-tagged).  Scene text may
   be Roman Urdu / Urdu / English mixed: common Roman-Urdu visual words
   are mapped to English via :data:`URDU_VISUAL_MAP` and non-visual
   filler (postpositions, auxiliaries, generic verbs) is dropped.
2. **Synonym / subject query** -- a broader subject-level variant: the
   scene's main subject plus a mood/concept word drawn from the niche
   profile's visual vocabulary when one is present.
3. **Niche signature query** (optional) -- the profile's standing
   B-roll vocabulary phrase, included ONLY when steps 1-2 are very
   short or empty, so a thin scene still gets a coherent search.

Rules enforced for every returned query: lowercase, deduplicated,
at most ~6 words, no brand names (small blocklist), no people's names,
and the ladder is NEVER empty -- when nothing at all is extractable it
falls back to ``"<niche name> background"``.

The scene is normally a ``pipeline/scenes`` ``SceneRecord`` and the
profile a ``niches.NicheProfile``, but every field read is defensive
(``getattr`` / dict ``.get``), so plain objects and dicts work too.
"""

from __future__ import annotations

import re
from typing import Any, Optional

__all__ = ["BRAND_BLOCKLIST", "URDU_VISUAL_MAP", "build_query_ladder"]

#: Hard caps from the mission / section 24 (one call per ladder step).
MAX_QUERIES = 3
MAX_QUERY_WORDS = 6

# ---------------------------------------------------------------------------
# Roman-Urdu -> English visual vocabulary
# ---------------------------------------------------------------------------
# Single-word English values only, so token order in the source sentence
# is preserved in the query (stock search is effectively bag-of-words).
URDU_VISUAL_MAP: dict[str, str] = {
    # Nature / setting
    "samundar": "ocean",
    "samandar": "ocean",
    "samandari": "ocean",
    "gehrai": "depth",
    "gehra": "deep",
    "gehri": "deep",
    "pani": "water",
    "paani": "water",
    "machli": "fish",
    "machliyan": "fish",
    "roshni": "light",
    "andhera": "darkness",
    "andheraa": "darkness",
    "pahar": "mountain",
    "pahari": "mountain",
    "barish": "rain",
    "jungle": "forest",
    "jangal": "forest",
    "sheher": "city",
    "shahar": "city",
    "gaon": "village",
    "khana": "food",
    "kitaab": "book",
    "kitab": "book",
    "sitare": "stars",
    "sitara": "star",
    "chaand": "moon",
    "chand": "moon",
    "sooraj": "sun",
    "suraj": "sun",
    "aasmaan": "sky",
    "aasman": "sky",
    "badal": "clouds",
    "baadal": "clouds",
    "hawa": "wind",
    "aag": "fire",
    "zameen": "land",
    "darakht": "tree",
    "ped": "tree",
    "phool": "flower",
    "nadi": "river",
    "dariya": "river",
    "jheel": "lake",
    "barf": "snow",
    "registan": "desert",
    "sehra": "desert",
    "dhoop": "sunlight",
    "saaya": "shadow",
    "khaamoshi": "silence",
    "khamoshi": "silence",
    # Places / objects
    "masjid": "mosque",
    "qila": "fort",
    "mahal": "palace",
    "pul": "bridge",
    "sadak": "road",
    "bazaar": "market",
    "bazar": "market",
    "dukaan": "shop",
    "dukan": "shop",
    "ghar": "house",
    "makaan": "house",
    "kamra": "room",
    "darwaza": "door",
    "kashti": "boat",
    "gaari": "car",
    "ghora": "horse",
    "kutta": "dog",
    "billi": "cat",
    "parinda": "bird",
    "parinde": "birds",
    "haveli": "mansion",
    "purana": "old",
    "purani": "old",
    # Concepts that still search well as stock
    "khauf": "fear",
    "umeed": "hope",
    "zindagi": "life",
    "maut": "death",
    "waqt": "time",
    "tareekh": "history",
    "jang": "war",
    "aman": "peace",
    "mohabbat": "love",
    "khushi": "happiness",
    "raaz": "mystery",
    "safar": "journey",
}

# ---------------------------------------------------------------------------
# Brand blocklist (section 24: exclude recognisable brands/logos)
# ---------------------------------------------------------------------------
#: Multi-word brand phrases, removed before tokenising.
_BRAND_PHRASES: tuple[str, ...] = (
    "coca cola",
    "coca-cola",
    "apple logo",
    "mcdonald's",
    "mcdonalds",
)

#: Single-token brands, dropped wherever they appear.
_BRAND_TOKENS: frozenset[str] = frozenset({
    "pepsi", "coke", "nike", "adidas", "puma", "starbucks", "google",
    "facebook", "instagram", "youtube", "netflix", "amazon", "microsoft",
    "samsung", "toyota", "honda", "disney", "mcdonalds", "logo", "logos",
})

#: Public view of the blocklist (phrases + tokens), for tests / audits.
BRAND_BLOCKLIST: frozenset[str] = frozenset(_BRAND_TOKENS) | frozenset(_BRAND_PHRASES)

# ---------------------------------------------------------------------------
# People's names (never valid stock queries for faceless B-roll)
# ---------------------------------------------------------------------------
_NAME_TOKENS: frozenset[str] = frozenset({
    # Common South-Asian names appearing in narration
    "ahmed", "ali", "muhammad", "mohammad", "mohammed", "fatima",
    "ayesha", "aisha", "zainab", "hassan", "hussain", "usman", "bilal",
    "hamza", "imran", "shahid", "tariq", "khan", "malik", "raza",
    "iqbal", "jinnah",
    # Common Western first / last names
    "john", "david", "michael", "james", "robert", "smith", "johnson",
    "williams", "brown", "jones", "miller", "davis", "wilson",
    "taylor", "anderson", "thomas", "jackson", "harris",
    "martin", "garcia", "martinez", "clark", "lewis", "walker",
    "allen", "wright", "scott", "adams", "nelson",
    # NOTE: ambiguous surnames that are also strong visual words
    # (green / brown / white / young / king / baker) are deliberately
    # NOT blocked here -- the capitalised-pair heuristic still catches
    # them when they appear as part of a full name.
})

#: Visual words that may legitimately be capitalised mid-sentence
#: ("Great Wall", "Red Fort") and must survive the name heuristic.
_PROTECTED_VISUAL_WORDS: frozenset[str] = frozenset({
    "wall", "tower", "bridge", "castle", "palace", "temple", "church",
    "mosque", "river", "lake", "mount", "fort", "falls", "park",
})

# ---------------------------------------------------------------------------
# Filler: stopwords + non-visual verbs (English + Roman Urdu)
# ---------------------------------------------------------------------------
_STOPWORDS: frozenset[str] = frozenset({
    # English function words
    "a", "an", "the", "in", "on", "at", "of", "to", "from", "by",
    "with", "without", "is", "are", "was", "were", "be", "been",
    "being", "am", "and", "or", "but", "if", "then", "than", "so",
    "as", "it", "its", "this", "that", "these", "those", "there",
    "here", "he", "she", "they", "we", "you", "i", "his", "her",
    "their", "our", "your", "my", "who", "whom", "which", "what",
    "when", "where", "why", "how", "all", "any", "some", "no", "not",
    "very", "just", "also", "into", "over", "under", "between",
    "through", "during", "before", "after", "up", "down", "out",
    "about", "against", "can", "could", "will", "would", "shall",
    "should", "may", "might", "must", "do", "does", "did", "have",
    "has", "had",
    # Number words search poorly as stock terms
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten",
    # Roman-Urdu function words (postpositions, pronouns, auxiliaries)
    "ki", "ka", "ke", "mein", "mai", "se", "ko", "ne", "par", "pe",
    "aur", "ya", "lekin", "magar", "phir", "bhi", "hi", "toh", "ab",
    "jab", "tab", "kab", "kya", "kyun", "kyon", "kaise", "kahan",
    "idhar", "udhar", "bahut", "bohat", "zyada", "kam", "ek", "yeh",
    "ye", "woh", "wo", "is", "us", "in", "un", "mera", "tera",
    "uska", "iska", "apna", "kuch", "koi", "sab", "har", "wala",
    "wali", "wale", "jaisa", "jaisi", "tarah", "liye", "baad",
    "pehle", "andar", "bahar", "hai", "hain", "tha", "thi", "thay",
    "ho", "hoga", "hogi",
})

_DROP_VERBS: frozenset[str] = frozenset({
    # English non-visual verbs / narration glue
    "becomes", "become", "became", "goes", "going", "gone", "comes",
    "came", "gets", "get", "got", "makes", "make", "made", "takes",
    "take", "took", "says", "said", "tells", "told", "knows", "knew",
    "thinks", "thought", "feels", "felt", "seems", "seemed", "ends",
    "end", "ended", "starts", "start", "begins", "began", "happens",
    "happened", "gives", "gave", "keeps", "kept", "puts", "put",
    "went", "see", "sees", "saw", "look", "looks", "looked",
    # Roman-Urdu verb forms (the visual content is in the nouns)
    "hota", "hoti", "hote", "hua", "hui", "hue", "gaya", "gayi",
    "gaye", "raha", "rahi", "rahe", "karta", "karti", "karte",
    "kiya", "kiye", "diya", "diye", "liya", "liye", "aata", "aati",
    "aaya", "aayi", "aaye", "jata", "jati", "jate", "jaata", "jaati",
    "khatm", "dikhai", "deta", "deti", "dete", "leta", "leti",
    "milta", "milti", "rehta", "rehti", "lagta", "lagti", "chala",
    "chali", "gira", "giri", "utha", "uthi",
})

#: Media-production words that carry no visual search value.
_MEDIA_WORDS: frozenset[str] = frozenset({
    "stock", "footage", "video", "videos", "image", "images",
    "graphic", "graphics", "animation", "animations", "recording",
    "recordings", "overlay", "overlays", "effect", "effects",
    "style", "zoom", "text",
})

#: Atmosphere / concept words usable as the step-2 mood term.
_MOOD_WORDS: frozenset[str] = frozenset({
    "dark", "darkness", "mystery", "mysterious", "calm", "soft",
    "golden", "ancient", "old", "fog", "mist", "night", "sunrise",
    "sunset", "dramatic", "peaceful", "serene", "moody", "deep",
    "underwater", "slow", "bright", "silent", "silence", "fear",
    "hope", "dream", "stormy", "rainy", "snowy", "lonely", "sacred",
})

#: Concrete subjects: the step-2 query is anchored on the first of
#: these found in the scene (falling back to the first scene token).
_SUBJECT_NOUNS: frozenset[str] = frozenset({
    "ocean", "sea", "water", "mountain", "forest", "jungle", "city",
    "village", "river", "lake", "desert", "sky", "sun", "moon",
    "stars", "star", "rain", "snow", "fire", "food", "book", "fish",
    "light", "darkness", "house", "mosque", "fort", "palace",
    "bridge", "road", "market", "shop", "room", "door", "tree",
    "flower", "clouds", "cloud", "boat", "ship", "car", "horse",
    "dog", "cat", "bird", "birds", "beach", "island", "valley",
    "waterfall", "cave", "garden", "field", "farm", "school",
    "hospital", "laboratory", "library", "kitchen", "waves", "wave",
    "sand", "stone", "rock", "ice", "volcano", "earth", "space",
    "planet", "galaxy", "wind", "land", "mansion",
    "architecture", "nature", "sunlight", "shadow", "silence",
    "fear", "hope", "life", "death", "time", "history", "war",
    "peace", "love", "happiness", "mystery", "journey",
})

#: Last-resort mood per scene class, used only when neither the scene
#: nor the profile supplies a second step-2 term.
_CLASS_MOOD: dict[str, str] = {
    "abstract_concept": "mystery",
    "hero": "dramatic",
}

_TOKEN_RE = re.compile(r"[a-z][a-z0-9]*")
_CASED_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")

_SCENE_TEXT_FIELDS = (
    "visual_description", "visual_desc", "visual", "scene_description",
    "description", "visual_prompt", "generation_prompt", "stock_query",
    "source_sentence", "narration_text", "narration", "text",
    "sentence", "script_text", "body", "content", "overlay_text",
)
_PROFILE_VISUAL_FIELDS = (
    "visual_vocabulary", "b_roll_vocabulary", "standing_b_roll",
    "broll_vocabulary", "visual_style", "visual_hints",
)
_PROFILE_SIGNATURE_FIELDS = (
    "signature_query", "signature", "standing_b_roll",
    "b_roll_vocabulary", "visual_vocabulary", "broll_vocabulary",
)
_PROFILE_NAME_FIELDS = ("name", "niche_name", "custom_niche_name", "profile_id", "niche_id", "niche")


# ---------------------------------------------------------------------------
# Defensive field access (SceneRecord / NicheProfile / dict / namespace)
# ---------------------------------------------------------------------------

def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if obj is None:
            break
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None and (not isinstance(value, str) or value.strip()):
            return value
    return default


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    # Enums (SceneClass) and other scalars stringify usefully.
    inner = getattr(value, "value", None)
    if isinstance(inner, str):
        return inner
    return str(value)


def _scene_text(scene: Any) -> str:
    """Best scene text: visual description first, then narration."""
    if scene is None:
        return ""
    if isinstance(scene, str):
        return scene
    return _as_text(_get(scene, *_SCENE_TEXT_FIELDS, default=""))


def _scene_class_key(scene: Any) -> str:
    raw = _as_text(_get(scene, "scene_class", "class", default=""))
    return raw.strip().lower().replace("-", "_").rsplit(".", 1)[-1]


# ---------------------------------------------------------------------------
# Token pipeline
# ---------------------------------------------------------------------------

def _name_tokens_in(text: str) -> set[str]:
    """People-name tokens detected in the *original* (cased) text.

    Two signals: the built-in name list (applied later, token by token)
    and a capitalisation heuristic -- a run of 2+ Capitalised tokens
    that does not start the text is treated as a person's name
    ("... of John Smith in ..."), unless a run member is a protected
    visual word ("Great Wall").
    """
    cased = _CASED_TOKEN_RE.findall(text or "")
    drop: set[str] = set()
    run: list[str] = []
    run_start = 0

    def flush() -> None:
        if len(run) >= 2 and run_start > 0:
            lowered = [t.lower().strip("'-") for t in run]
            if not any(t in _PROTECTED_VISUAL_WORDS for t in lowered):
                drop.update(lowered)

    for idx, token in enumerate(cased):
        if token[:1].isupper() and not token.isupper():
            if not run:
                run_start = idx
            run.append(token)
        else:
            flush()
            run = []
    flush()
    return drop


def _clean_tokens(text: str, *, drop_media: bool = False) -> list[str]:
    """English visual tokens from mixed-language ``text``.

    Translates Roman Urdu via :data:`URDU_VISUAL_MAP`, strips brands
    and people's names, drops filler, dedupes preserving order.
    Non-Latin script (e.g. Urdu script) yields no tokens -- callers
    treat that as "nothing extractable" and fall back.
    """
    if not text:
        return []
    lowered = text.lower().replace("’", "'")
    for phrase in sorted(_BRAND_PHRASES, key=len, reverse=True):
        lowered = re.sub(
            r"\b" + re.escape(phrase).replace(r"\-", r"[-\s]") + r"\b",
            " ",
            lowered,
        )
    names = _name_tokens_in(text)
    out: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(lowered):
        token = raw.strip("'")
        if not token or token in names or token in _NAME_TOKENS:
            continue
        if token in _BRAND_TOKENS:
            continue
        mapped = URDU_VISUAL_MAP.get(token)
        candidates = mapped.split() if mapped else [token]
        for candidate in candidates:
            if candidate in _STOPWORDS or candidate in _DROP_VERBS:
                continue
            if drop_media and candidate in _MEDIA_WORDS:
                continue
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def _cap(words: list[str]) -> str:
    return " ".join(words[:MAX_QUERY_WORDS])


# ---------------------------------------------------------------------------
# Profile-derived vocabulary
# ---------------------------------------------------------------------------

def _profile_visual_tokens(profile: Any) -> list[str]:
    """Ordered visual tokens from the profile's visual vocabulary."""
    if profile is None:
        return []
    if isinstance(profile, str):
        return _clean_tokens(profile, drop_media=True)
    tokens: list[str] = []
    seen: set[str] = set()
    for field in _PROFILE_VISUAL_FIELDS:
        value = _get(profile, field)
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            for token in _clean_tokens(_as_text(item), drop_media=True):
                if token not in seen:
                    seen.add(token)
                    tokens.append(token)
    return tokens


def _profile_mood(profile: Any) -> Optional[str]:
    """First mood/concept word in the profile's visual vocabulary,
    falling back to its tone adjectives."""
    for token in _profile_visual_tokens(profile):
        if token in _MOOD_WORDS:
            return token
    tone = _get(profile, "tone") if profile is not None else None
    if isinstance(tone, (list, tuple)):
        for item in tone:
            for token in _clean_tokens(_as_text(item)):
                if token in _MOOD_WORDS:
                    return token
    elif isinstance(tone, str):
        for token in _clean_tokens(tone):
            if token in _MOOD_WORDS:
                return token
    return None


def _signature_query(profile: Any) -> Optional[str]:
    """The profile's standing B-roll phrase (ladder step 3), if any.

    Explicit signature/vocabulary fields win; otherwise the first
    usable comma/+ separated phrase of ``visual_style`` (negated
    phrases such as "no faces of ..." are skipped, media-production
    words are stripped, and a phrase must carry a subject/mood word
    or at least two content words to qualify).
    """
    if profile is None or isinstance(profile, str):
        return None
    for field in _PROFILE_SIGNATURE_FIELDS:
        value = _get(profile, field)
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            text = _as_text(item)
            for segment in re.split(r"[,;]", text):
                tokens = _clean_tokens(segment, drop_media=True)
                if tokens:
                    return _cap(tokens)
    style = _as_text(_get(profile, "visual_style", "visual_hints", default=""))
    for phrase in re.split(r"[,+]", style):
        cleaned = phrase.strip()
        if not cleaned or cleaned.lower().startswith(("no ", "no-")):
            continue
        tokens = _clean_tokens(cleaned, drop_media=True)
        if not tokens:
            continue
        if len(tokens) >= 2 or tokens[0] in _SUBJECT_NOUNS or tokens[0] in _MOOD_WORDS:
            return _cap(tokens[:4])
    return None


def _niche_fallback(profile: Any) -> str:
    """``"<niche name> background"`` -- the never-empty last resort."""
    name_text = ""
    if isinstance(profile, str):
        name_text = profile
    elif profile is not None:
        name_text = _as_text(_get(profile, *_PROFILE_NAME_FIELDS, default=""))
    # "Facts / Curiosity" -> the head label names the niche.
    head = name_text.split("/")[0]
    tokens = [t for t in _clean_tokens(head) if t != "background"]
    if not tokens and profile is not None and not isinstance(profile, str):
        pid = _as_text(_get(profile, "profile_id", "niche_id", default=""))
        tokens = [t for t in _clean_tokens(pid) if t != "background"]
    if not tokens:
        tokens = ["nature"]
    return _cap(tokens[: MAX_QUERY_WORDS - 1] + ["background"])


# ---------------------------------------------------------------------------
# Ladder construction
# ---------------------------------------------------------------------------

def _subject_query(
    tokens: list[str], scene: Any, profile: Any
) -> Optional[str]:
    """Step 2: main subject + mood/concept word (broader variant)."""
    if not tokens:
        return None
    subject = next((t for t in tokens if t in _SUBJECT_NOUNS), tokens[0])
    mood = _profile_mood(profile)
    if mood and mood != subject:
        return _cap([subject, mood])
    # No profile mood: broaden with the next distinct subject noun,
    # then any next distinct token, then a scene-class concept word.
    for token in tokens:
        if token != subject and token in _SUBJECT_NOUNS:
            return _cap([subject, token])
    for token in tokens:
        if token != subject:
            return _cap([subject, token])
    class_mood = _CLASS_MOOD.get(_scene_class_key(scene))
    if class_mood and class_mood != subject:
        return _cap([subject, class_mood])
    return subject


def build_query_ladder(scene: Any, profile: Any = None) -> list[str]:
    """Return 1-3 English stock-search queries for ONE scene.

    Args:
        scene: A ``SceneRecord`` (its ``source_sentence`` /
            ``narration_text`` / ``scene_class`` are read), a dict, a
            plain object with any of the known text fields, a raw
            string, or ``None``.
        profile: A ``NicheProfile``, dict, plain object or ``None``;
            its ``visual_style`` / visual vocabulary supplies the
            step-2 mood word and the step-3 signature phrase, and its
            ``name`` / ``profile_id`` supplies the fallback.

    Returns:
        Lowercase, deduplicated queries of at most 6 words each,
        most specific first; never empty (see module docstring).
    """
    tokens = _clean_tokens(_scene_text(scene))

    ladder: list[str] = []
    if tokens:
        # Step 1 -- exact visual query.
        ladder.append(_cap(tokens))
        # Step 2 -- synonym / subject query.
        step2 = _subject_query(tokens, scene, profile)
        if step2 and step2 not in ladder:
            ladder.append(step2)

    # Step 3 -- niche signature, ONLY for very short / empty scenes.
    if len(tokens) <= 2:
        signature = _signature_query(profile)
        if signature and signature not in ladder:
            ladder.append(signature)

    if not ladder:
        ladder.append(_niche_fallback(profile))

    # Final enforcement: lowercase (by construction), deduped,
    # word-capped, at most MAX_QUERIES, never empty.
    seen: set[str] = set()
    result: list[str] = []
    for query in ladder:
        words = query.split()[:MAX_QUERY_WORDS]
        normalised = " ".join(words).strip().lower()
        if normalised and normalised not in seen:
            seen.add(normalised)
            result.append(normalised)
    return result[:MAX_QUERIES] or ["nature background"]
