"""Deterministic comment analyser — the labelled fallback engine.

Pure functions, no I/O, fully reproducible: the same comments always
produce the same bands, themes, questions, topics and spam flags.

Methods (all documented in docs/COMMENTS.md):
* Sentiment — ``lexicon_v1``: English + Roman Urdu word lexicons with
  a one-token negation flip (``not good``, ``nahi acha``).  Score > 0
  positive, < 0 negative, else neutral.  Questions are classified
  BEFORE sentiment (they form their own band).
* Questions — a sentence ending in ``?`` or starting with an
  interrogative (English: what/why/how/…; Roman Urdu: kya/kaise/
  kab/kahan/kyun/kon/kitna/…).  The extracted text is the verbatim
  sentence, never paraphrased.
* Themes — token-frequency: content tokens (stopwords removed,
  EN + Roman Urdu) ranked by document frequency; each top token is a
  theme and a comment joins every theme whose token it contains.
* Praise / complaint topics — the same token-frequency method run
  over the positive / negative subsets respectively (label is the
  recurring content token viewers praise or complain about).
* Spam — ``heuristics_v1``: URLs/links, known spam phrases, duplicate
  text within the set, ALL-CAPS ratio, character/word repetition.
"""

from __future__ import annotations

import re
from collections import Counter

from .schema import CommentInput, SpamFlag, ThemeCluster, TopicItem, TopQuestion

__all__ = [
    "POSITIVE_WORDS",
    "NEGATIVE_WORDS",
    "classify_comment",
    "is_question_text",
    "extract_questions",
    "spam_reasons",
    "token_themes",
    "topic_items",
]

_TOKEN_RE = re.compile(r"[a-zA-Z']+")
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")

POSITIVE_WORDS = {
    # English
    "great", "amazing", "awesome", "excellent", "best", "beautiful",
    "love", "loved", "loves", "informative", "helpful", "thanks",
    "thank", "fantastic", "wonderful", "superb", "brilliant", "nice",
    "good", "interesting", "wow", "perfect", "lovely", "inspiring",
    "amazed", "favorite", "favourite", "enjoyed", "enjoy", "cool",
    # Roman Urdu
    "zabardast", "behtareen", "acha", "achi", "achha", "achhi",
    "khoobsurat", "lajawab", "shukriya", "kamaal", "kamal", "pasand",
    "dilchasp", "umda",
}

NEGATIVE_WORDS = {
    # English
    "bad", "worst", "boring", "bored", "terrible", "awful", "hate",
    "hated", "poor", "disappointing", "disappointed", "waste",
    "confusing", "confused", "wrong", "horrible", "useless", "slow",
    "annoying", "stupid", "dumb", "fake", "misleading", "rubbish",
    # Roman Urdu
    "bakwas", "bura", "buri", "ghatiya", "bekar", "bekaar",
    "fazool", "ghalat",
}

NEGATIONS = {"not", "no", "never", "nahi", "nahin", "na"}

INTERROGATIVE_STARTS = {
    # English
    "what", "why", "how", "when", "where", "who", "whom", "which",
    "can", "could", "do", "does", "did", "is", "are", "was", "were",
    "will", "would", "should", "has", "have",
    # Roman Urdu
    "kya", "kaise", "kab", "kahan", "kidhar", "kyun", "kyon", "kon",
    "kaun", "kitna", "kitni", "kitne", "kis", "kisko", "kisne",
}

STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "and", "or", "is",
    "are", "was", "were", "with", "for", "you", "your", "this",
    "that", "it", "its", "i", "we", "they", "he", "she", "my",
    "me", "am", "be", "been", "so", "very", "really", "just",
    "video", "videos", "channel", "please", "plz", "sir", "bro",
    # Roman Urdu function words
    "ke", "ki", "ka", "aur", "mein", "hai", "hain", "ye", "yeh",
    "woh", "wo", "ne", "se", "ko", "par", "bhi", "tha", "thi",
    "ho", "hoga", "kya", "bohat", "bohut", "bahut", "aap", "ap",
    "mera", "meri", "mere", "hum", "tum",
}

SPAM_PHRASES = (
    "subscribe to my channel", "sub4sub", "sub 4 sub",
    "check out my channel", "visit my channel", "earn money",
    "make money online", "crypto", "bitcoin investment",
    "free followers", "free subscribers", "whatsapp me",
    "click the link", "work from home and earn", "investment opportunity",
)

_LINK_RE = re.compile(
    r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|io|xyz|click|top|site|online)\b)",
    re.IGNORECASE,
)


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def is_question_text(text: str) -> bool:
    """True when the text contains a question sentence."""
    return bool(extract_question_sentences(text))


def extract_question_sentences(text: str) -> list[str]:
    out: list[str] = []
    for raw in _SENTENCE_RE.findall(text or ""):
        s = raw.strip()
        if not s:
            continue
        words = tokens(s)
        if s.endswith("?") or (words and words[0] in INTERROGATIVE_STARTS and len(words) >= 2):
            out.append(s)
    return out


def sentiment_of(text: str) -> str:
    """'positive' | 'negative' | 'neutral' by lexicon_v1."""
    toks = tokens(text)
    score = 0
    for i, tok in enumerate(toks):
        negated = i > 0 and toks[i - 1] in NEGATIONS
        if tok in POSITIVE_WORDS:
            score += -1 if negated else 1
        elif tok in NEGATIVE_WORDS:
            score += 1 if negated else -1
    if score > 0:
        return "positive"
    if score < 0:
        return "negative"
    return "neutral"


def classify_comment(comment: CommentInput) -> str:
    """Band for one comment: 'question' takes precedence, else sentiment."""
    if is_question_text(comment.text):
        return "question"
    return sentiment_of(comment.text)


def spam_reasons(comment: CommentInput, seen_texts: Counter) -> list[str]:
    """Heuristic spam/abuse reasons for one comment (empty = not spam).

    ``seen_texts`` is a Counter of normalised texts already seen in
    the analysed set (for duplicate detection); the caller owns it.
    """
    reasons: list[str] = []
    text = comment.text or ""
    low = text.lower()
    if _LINK_RE.search(text):
        reasons.append("contains_link")
    for phrase in SPAM_PHRASES:
        if phrase in low:
            reasons.append(f"spam_phrase:{phrase}")
            break
    norm = " ".join(tokens(text))
    if norm and seen_texts[norm] >= 1 and len(norm) >= 12:
        reasons.append("duplicate_text")
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 10:
        caps = sum(1 for c in letters if c.isupper())
        if caps / len(letters) > 0.7:
            reasons.append("excessive_caps")
    toks = tokens(text)
    if len(toks) >= 6:
        top = Counter(toks).most_common(1)[0][1]
        if top / len(toks) > 0.5:
            reasons.append("repetitive_words")
    if re.search(r"(.)\1{7,}", text):
        reasons.append("character_spam")
    return reasons


def flag_spam(comments: list[CommentInput]) -> tuple[list[SpamFlag], set[str]]:
    """Return (flags, spam_comment_ids) for a comment set, in order."""
    seen: Counter = Counter()
    flags: list[SpamFlag] = []
    spam_ids: set[str] = set()
    for c in comments:
        reasons = spam_reasons(c, seen)
        seen[" ".join(tokens(c.text))] += 1
        if reasons:
            spam_ids.add(c.id)
            for reason in reasons:
                flags.append(
                    SpamFlag(
                        comment_id=c.id,
                        video_id=c.video_id,
                        channel_id=c.channel_id,
                        reason=reason,
                    )
                )
    return flags, spam_ids


def extract_questions(comments: list[CommentInput]) -> list[TopQuestion]:
    """Verbatim viewer questions, ranked by likes then replies.

    This is the deterministic implementation of the TOP-QUESTIONS
    SEAM (see app.comment_intel.engine.top_questions_for_channel and
    docs/COMMENTS.md).
    """
    questions: list[TopQuestion] = []
    for c in comments:
        for sentence in extract_question_sentences(c.text):
            questions.append(
                TopQuestion(
                    question_text=sentence,
                    video_id=c.video_id,
                    comment_id=c.id,
                    like_count=c.like_count or 0,
                )
            )
    by_id = {c.id: c for c in comments}
    questions.sort(
        key=lambda q: (-(q.like_count or 0), -(by_id[q.comment_id].reply_count or 0), q.comment_id)
    )
    return questions


def _content_tokens(text: str) -> list[str]:
    return [t for t in tokens(text) if len(t) >= 4 and t not in STOPWORDS]


def token_themes(comments: list[CommentInput], *, max_themes: int = 8) -> list[ThemeCluster]:
    """Token-frequency theme clusters over the given comments."""
    if not comments:
        return []
    doc_freq: Counter = Counter()
    per_comment: dict[str, set[str]] = {}
    for c in comments:
        toks = set(_content_tokens(c.text))
        per_comment[c.id] = toks
        doc_freq.update(toks)
    if not doc_freq:
        return []
    # A theme needs >=2 comments, unless the whole set is tiny.
    min_df = 2 if len(comments) >= 4 else 1
    ranked = [t for t, n in doc_freq.most_common() if n >= min_df]
    if not ranked:  # fall back to the single most frequent token
        ranked = [doc_freq.most_common(1)[0][0]]
    themes: list[ThemeCluster] = []
    for tok in ranked[:max_themes]:
        ids = [cid for cid, toks in per_comment.items() if tok in toks]
        themes.append(
            ThemeCluster(
                label=tok,
                comment_ids=ids,
                share=round(len(ids) / len(comments), 4),
            )
        )
    return themes


def topic_items(comments: list[CommentInput], *, max_topics: int = 6) -> list[TopicItem]:
    """Frequent content tokens in a subset, as praise/complaint topics."""
    if not comments:
        return []
    doc_freq: Counter = Counter()
    per_comment: dict[str, set[str]] = {}
    for c in comments:
        toks = set(_content_tokens(c.text))
        per_comment[c.id] = toks
        doc_freq.update(toks)
    items: list[TopicItem] = []
    for tok, _n in doc_freq.most_common(max_topics):
        ids = [cid for cid, toks in per_comment.items() if tok in toks]
        if ids:
            items.append(TopicItem(label=tok, comment_ids=ids, count=len(ids)))
    return items
