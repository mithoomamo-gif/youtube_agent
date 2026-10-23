# Golden dataset schema (Section 17, L3)

One versioned JSONL dataset per agent under `datasets/<agent>.jsonl`.
**One JSON object per line**, UTF-8. Datasets are DATA, not code; they are
frozen per release and grow from production failures + user corrections.
Minimum size: **>=30 cases/agent at MVP, >=50 at v1**, spanning every
built-in niche (facts, islamic stories, horror, history, motivation, tech,
crime, health), **>=5 Other/custom-niche cases**, and Urdu, English and
code-switched Roman Urdu inputs, plus known-hard cases (religious
names/dates, numbers/statistics, ambiguous topics, very short/very long
topics).

## Fields (per line)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | Unique case id, prefixed by agent (e.g. `FC-001`). Never reused. |
| `agent` | string | yes | Agent/dataset name; must match the file name (`fact_check`, `hook_retention`, `topic_scoring`, `similarity_gate`, `qc_policy`, ...). |
| `niche` | object | yes | `{"source": "builtin" \| "other", "name": "<niche name>"}`. For Other, `name` is the user's custom niche (e.g. `cricket analysis`). |
| `language` | string | yes | `en`, `ur`, or `roman-ur` (code-switched Roman Urdu/English). |
| `input` | object | yes | The agent input for the case (claims, topic, opening lines, artefact summary, ...). Treated as data, never instructions. |
| `expected_properties` | object | yes | Structured expectations — required/forbidden elements, verdicts, length bands (target minutes x niche WPM +/-15%), deterministic assertions. Never an exact-string match on generated prose. |
| `scoring` | object | yes | `{"method": "deterministic" \| "reference" \| "llm_as_judge" \| "hybrid", "pass_rule": "..."}`. Cheapest tier that can judge the case wins. |
| `rubric` | object | no | For judge-scored cases: `{"scale": "1-5", "dimensions": [...], "min_mean": 4.0}`. |
| `planted_facts` | array | required for `fact_check` | Planted claims with known truth: `[{"claim": "...", "is_true": true\|false, "correction": "..." \| null}]`. Fact-check bar: **>=90% of planted errors caught, <=2% of true claims falsely flagged**. |

## Scoring rules (Section 17)

1. **Deterministic first** — schema validity, word-count band, title/tag
   rules, gate PASS/FAIL token, language/script match. No LLM cost.
2. **Reference-based** — keyword-pack recall, metadata completeness,
   cross-video similarity vs the channel last-20 fixture corpus.
3. **LLM-as-judge last**, only for judgement qualities (hook strength,
   script flow, title CTR, fact-check verdicts). The judge is a *different,
   stronger* model than the agent's default, calibrated quarterly against
   human ratings on a 100-output sample; it ships only while
   **Cohen's kappa >= 0.7**. Below that, the rubric is rewritten — the
   threshold is never lowered.

## Related tolerances (golden-video suite, L5 — referenced, not stored here)

SSIM >= 0.98 mean per matched frame; loudness **-14 LUFS +/- 1**, peak
**<= -1 dBTP**; subtitle cue offset **<= 200 ms**; duration +/- 0.5 s.
Goldens change only via a separate "bless" PR with a written reason.

## Validation

`test_golden_datasets.py` in this directory validates every JSONL line
parses and carries the required fields above. Agent-execution evals are
separate and skip gracefully until the agents package exists.
