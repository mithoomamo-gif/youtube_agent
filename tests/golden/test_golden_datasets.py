"""Golden-dataset validation (Section 17, L3).

Two kinds of tests live here:
1. Dataset validation -- every JSONL line parses and matches SCHEMA.md.
   These MUST pass on the seed data.
2. Agent-execution evals -- run agents against the datasets and score them.
   These skip gracefully until the agents package exists.
"""
import json
import pathlib

import pytest

pytestmark = pytest.mark.golden

DATASETS_DIR = pathlib.Path(__file__).parent / "datasets"
REQUIRED_FIELDS = {"id", "agent", "niche", "language", "input",
                   "expected_properties", "scoring"}
ALLOWED_SCORING_METHODS = {"deterministic", "reference", "llm_as_judge", "hybrid"}
ALLOWED_LANGUAGES = {"en", "ur", "roman-ur"}


def _load_all_cases():
    cases = []
    files = sorted(DATASETS_DIR.glob("*.jsonl"))
    assert files, "no golden dataset files found"
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(f"{path.name}:{lineno} is not valid JSON: {exc}") from exc
            obj["_file"] = path.name
            obj["_line"] = lineno
            cases.append(obj)
    return cases


CASES = _load_all_cases()


def test_datasets_exist_for_key_agents():
    names = {p.stem for p in DATASETS_DIR.glob("*.jsonl")}
    assert {"fact_check", "hook_retention", "topic_scoring",
            "similarity_gate", "qc_policy"} <= names


def test_every_line_matches_schema():
    for c in CASES:
        missing = REQUIRED_FIELDS - set(c)
        assert not missing, f"{c['_file']}:{c['_line']} missing fields {missing}"
        assert isinstance(c["niche"], dict) and c["niche"].get("source") in {"builtin", "other"}
        assert c["niche"].get("name"), f"{c['id']}: niche.name empty"
        assert c["language"] in ALLOWED_LANGUAGES, f"{c['id']}: bad language {c['language']}"
        assert isinstance(c["input"], dict) and c["input"], f"{c['id']}: input empty"
        assert isinstance(c["expected_properties"], dict) and c["expected_properties"], \
            f"{c['id']}: expected_properties empty"
        assert c["scoring"].get("method") in ALLOWED_SCORING_METHODS, \
            f"{c['id']}: bad scoring.method {c['scoring'].get('method')}"
        # agent field must match the dataset file it lives in
        assert c["_file"] == f"{c['agent']}.jsonl", \
            f"{c['id']}: agent {c['agent']} does not match file {c['_file']}"


def test_case_ids_are_unique():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate golden case ids"


def test_fact_check_cases_carry_planted_facts():
    fc = [c for c in CASES if c["agent"] == "fact_check"]
    assert fc, "no fact_check cases"
    for c in fc:
        planted = c.get("planted_facts")
        assert isinstance(planted, list) and planted, f"{c['id']}: planted_facts missing"
        for p in planted:
            assert {"claim", "is_true", "correction"} <= set(p), f"{c['id']}: bad planted fact {p}"
        # every fact_check seed case must contain at least one planted error
        assert any(p["is_true"] is False for p in planted), \
            f"{c['id']}: no planted error to catch"


def test_coverage_other_niche_and_languages():
    assert sum(1 for c in CASES if c["niche"]["source"] == "other") >= 2, \
        "need at least 2 Other/custom-niche seed cases across the set"
    langs = {c["language"] for c in CASES}
    assert {"en", "ur", "roman-ur"} <= langs, f"language coverage incomplete: {langs}"


def test_cases_manifest_counts_match_datasets():
    manifest = json.loads((pathlib.Path(__file__).parent / "cases_manifest.json")
                          .read_text(encoding="utf-8"))
    for agent, info in manifest["agents"].items():
        actual = sum(1 for c in CASES if c["agent"] == agent)
        assert info["seed_cases"] == actual, \
            f"manifest says {info['seed_cases']} for {agent}, found {actual}"
        assert info["seed_cases"] >= 4, f"{agent}: seed should carry 4-8 cases"
    assert manifest["totals"]["seed_cases"] == len(CASES)


def test_agent_execution_evals_skip_without_agents_package():
    """Real agent evals: skipped (not failed) until the agents package lands."""
    agents = pytest.importorskip(
        "youtube_agent.agents",
        reason="agents package not built yet; golden dataset validation above is the seed-stage gate",
    )
    pytest.skip(f"agents package present ({agents!r}) but eval runner not wired in this stub")
