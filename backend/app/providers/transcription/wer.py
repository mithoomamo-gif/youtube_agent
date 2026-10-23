"""Word Error Rate (WER) — small hand-rolled implementation.

WER = (substitutions + deletions + insertions) / reference words,
computed with a classic Levenshtein alignment over word tokens after
normalisation (lower-casing, punctuation stripped, whitespace
collapsed).  No external dependency (``jiwer`` is intentionally NOT
required): the metric is ~40 lines and this keeps the live-test gate
hermetic apart from the model itself.
"""
from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[^\w\s']", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_words(text: str) -> list[str]:
    """Normalise text to a comparable word-token list."""
    cleaned = _PUNCT_RE.sub(" ", (text or "").lower())
    return [tok for tok in _WS_RE.split(cleaned.strip()) if tok]


def _edit_distance(ref: list[str], hyp: list[str]) -> int:
    """Levenshtein distance between two word sequences."""
    # Two-row dynamic programming; O(len(ref) * len(hyp)) time.
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i] + [0] * len(hyp)
        for j, hyp_word in enumerate(hyp, start=1):
            current[j] = min(
                previous[j] + 1,                        # deletion
                current[j - 1] + 1,                     # insertion
                previous[j - 1] + (ref_word != hyp_word),  # substitution
            )
        previous = current
    return previous[len(hyp)]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """WER of ``hypothesis`` against the ground-truth ``reference``.

    Returns 0.0 for a perfect match.  An empty reference scores 0.0
    when the hypothesis is also empty and 1.0 otherwise (there is no
    meaningful denominator).
    """
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return _edit_distance(ref, hyp) / len(ref)


__all__ = ["normalize_words", "word_error_rate"]
