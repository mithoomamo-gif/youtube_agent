"""Prompt safety layer.

Runs BEFORE any provider call. Every provider's terms leave trademark /
likeness infringement with the user, so prompts naming real brands,
celebrities or copyrighted characters are blocked here — and this product
is faceless, so requests for a recognisable real-person likeness are
blocked too (faceless-safety). A check returns a violation list; the
router turns a non-empty list into :class:`.errors.PromptBlockedError`.
"""

from __future__ import annotations

import re

#: Real brands / trademarks (lowercase phrases, word-boundary matched).
BRAND_DENYLIST: tuple[str, ...] = (
    "nike", "adidas", "coca-cola", "coca cola", "pepsi", "mcdonald's",
    "mcdonalds", "starbucks", "apple inc", "samsung galaxy", "gucci",
    "louis vuitton", "rolex", "ferrari", "lamborghini", "netflix logo",
    "youtube logo", "google logo",
)

#: Copyrighted characters / franchises.
CHARACTER_DENYLIST: tuple[str, ...] = (
    "mickey mouse", "minnie mouse", "donald duck", "pikachu", "pokemon",
    "harry potter", "hogwarts", "batman", "spider-man", "spiderman",
    "superman", "iron man", "captain america", "darth vader",
    "star wars", "marvel", "disney", "pixar", "frozen elsa", "shrek",
    "naruto", "goku", "mario bros", "sonic the hedgehog",
)

#: Celebrities / public figures whose likeness must never be generated.
CELEBRITY_DENYLIST: tuple[str, ...] = (
    "taylor swift", "tom cruise", "brad pitt", "angelina jolie",
    "shah rukh khan", "salman khan", "amitabh bachchan", "elon musk",
    "donald trump", "joe biden", "justin bieber", "selena gomez",
    "kim kardashian", "cristiano ronaldo", "lionel messi",
    "leonardo dicaprio", "will smith", "the rock", "dwayne johnson",
)

_ALL_TERMS: tuple[tuple[str, str], ...] = (
    tuple((t, "brand") for t in BRAND_DENYLIST)
    + tuple((t, "copyrighted character/franchise") for t in CHARACTER_DENYLIST)
    + tuple((t, "celebrity") for t in CELEBRITY_DENYLIST)
)

_LIKENESS_RE = re.compile(
    r"\b(?:photorealistic\s+)?(?:portrait|face|likeness|headshot)\s+of\s+"
    r"(?:a\s+|the\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})"
)


def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


_COMPILED: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (_term_pattern(term), term, kind) for term, kind in _ALL_TERMS
)


def check_prompt(prompt: str) -> list[str]:
    """Return a list of safety violations found in ``prompt``.

    Empty list = prompt is allowed. Each violation is a human-readable
    string naming the matched term and its category.
    """
    violations: list[str] = []
    if not prompt:
        return violations
    for pattern, term, kind in _COMPILED:
        if pattern.search(prompt):
            violations.append(f"{kind}: {term!r}")
    # Faceless-safety: no recognisable real-person likeness requests.
    m = _LIKENESS_RE.search(prompt)
    if m and not any(m.group(1).lower() in v for v in violations):
        violations.append(
            f"real-person likeness request: {m.group(0)!r} "
            "(faceless product — no recognisable real-person likenesses)"
        )
    return violations


def is_allowed(prompt: str) -> bool:
    return not check_prompt(prompt)
