"""QC fault-corpus validation (Section 17, L6).

Asserts the corpus parses, ids are unique, and every case is a labelled
must-fail. Detection-recall measurement itself belongs to the L6 harness
(broken media artefacts are generated there, hermetically).
"""
import json
import pathlib

CORPUS_PATH = pathlib.Path(__file__).parent / "cases.json"
ALLOWED_CATEGORIES = {"safety", "policy", "copyright", "technical"}
REQUIRED_FIELDS = {"id", "category", "fault", "expected_gate",
                   "expected_verdict", "notes"}


def _cases():
    data = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("cases"), list), \
        "cases.json must be an object with a 'cases' list"
    return data["cases"]


def test_corpus_parses_and_meets_mvp_size():
    cases = _cases()
    assert len(cases) >= 15, f"MVP bar is >=15 fault cases, found {len(cases)}"


def test_ids_unique_and_fields_complete():
    cases = _cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate fault-case ids"
    for c in cases:
        missing = REQUIRED_FIELDS - set(c)
        assert not missing, f"{c.get('id')}: missing fields {missing}"
        assert c["category"] in ALLOWED_CATEGORIES, \
            f"{c['id']}: bad category {c['category']}"
        assert c["fault"] and c["expected_gate"] and c["notes"], \
            f"{c['id']}: fault/expected_gate/notes must be non-empty"


def test_every_case_is_expected_fail():
    for c in _cases():
        assert c["expected_verdict"] == "FAIL", \
            f"{c['id']}: fault corpus contains only must-fails, got {c['expected_verdict']}"


def test_required_fault_families_present():
    text = " ".join(c["fault"].lower() for c in _cases())
    required_fragments = [
        "unlicensed music", "licence-ledger", "templated script",
        "disclaimer", "expert persona", "misleading thumbnail",
        "undisclosed realistic ai", "gore", "depiction-of-prophet",
        "wrong-channel", "duplicate upload", "loudness",
        "subtitle offset", "black/placeholder", "stock reuse",
    ]
    for frag in required_fragments:
        assert frag in text, f"required fault family missing from corpus: {frag}"
