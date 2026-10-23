# Comments Intelligence — Agent 28 (Round 5)

Comment analysis engine: `backend/app/comment_intel/`. Storage/intake:
`backend/app/comments/` (`CommentsStore`, migration `0006_comments` —
tables `comments`, `comment_sync_state`, `comment_analyses`,
`comment_drafts`, `comment_moderation_flags`).

## What the engine produces

A `CommentRollup` (`app.comment_intel.schema.CommentRollup`) per video,
and a per-channel rollup that is exactly the aggregate of its per-video
rollups (`aggregate_channel_rollup`):

- `comment_count` (all comments handed in), `analysed_count`
  (non-spam), `spam_count`
- `sentiment` — shares over analysed comments in four mutually
  exclusive bands summing to 1.0: **positive / neutral / negative /
  question**. Questions are their own band (they are the highest-value
  audience signal); a comment that asks a question is counted there
  before sentiment.
- `themes` — clusters `{label, comment_ids, share}`
- `top_questions` — extracted viewer questions (see the seam below)
- `praise_topics` / `complaint_topics` — `{label, comment_ids, count}`
- `spam_flags` — `{comment_id, video_id, channel_id, reason}` feeding
  the moderation queue
- `provenance` — see below; ALWAYS present
- `notes` — honest annotations (e.g. dropped invalid LLM output,
  LLM prompt truncation beyond 200 comments)

## Provenance (R4-003: no silent substitution)

```json
{
  "engine": "llm" | "deterministic",
  "model": "<physical model id>" | null,
  "generated_at": "<ISO-8601 UTC>",
  "sentiment_method": "lexicon_v1",
  "spam_method": "heuristics_v1",
  "fallback_reason": "<why the deterministic engine ran>" | null
}
```

- **Primary engine — LLM.** Theme clustering, question extraction and
  praise/complaint topics come from the existing LLM router
  (`app.providers.llm`, public `generate(LLMRequest)` interface),
  addressed by **tier T1 only** (Agent 28's registered default tier) —
  the engine never names a model, so the router's model-registry
  refusal and single-flight pacing apply untouched. The router's
  structured-output validation (`LLMCommentAnalysis` schema) is used.
- **Fallback — deterministic.** Runs ONLY when no LLM is configured
  (`fallback_reason="llm_not_configured"`) or the LLM call fails /
  returns unusable output (`fallback_reason="<ExceptionType>: <msg>"`).
  The result is labelled `engine="deterministic"`, `model=null` — an
  LLM failure never surfaces as an unlabelled swap. With
  `CommentAnalysisEngine(strict=True)`, an LLM failure instead raises
  the typed `CommentAnalysisError`.
- **Sentiment and spam are deterministic in BOTH modes** this round
  (lexicon / heuristics below) and are labelled as such in provenance
  either way, so their behaviour is identical and auditable whichever
  engine produced the theme layer.
- **Channel aggregation honesty:** a channel rollup is `engine="llm"`
  only when every contributing video rollup was LLM-produced by the
  same model; a mixed aggregate is labelled `deterministic` with a
  `fallback_reason` describing the mix.
- **LLM output validation:** theme/topic comment ids not in the
  analysed set are dropped; extracted questions whose text is not
  verbatim-contained in the cited comment are dropped (no fake
  quotes). Drops are counted in `rollup.notes`
  (`llm_unknown_comment_ids_dropped`, `llm_non_verbatim_questions_dropped`).

## Deterministic methods

- **Sentiment `lexicon_v1`** — English + Roman Urdu word lexicons
  (e.g. *great/amazing/zabardast/acha/shukriya* vs
  *boring/terrible/bakwas/ghatiya*), one-token negation flip
  (`not good`, `nahi acha`). Score > 0 positive, < 0 negative, else
  neutral.
- **Questions** — a sentence ending in `?` or starting with an
  interrogative (EN: what/why/how/when/…; Roman Urdu:
  kya/kaise/kab/kahan/kyun/kon/kitna/…). Question-band classification
  runs before sentiment.
- **Themes / praise / complaint topics** — token frequency over
  content tokens (stopwords removed, EN + Roman Urdu): top tokens
  become labels; a comment joins a theme whose token it contains.
  Praise topics run over the positive subset, complaint topics over
  the negative subset. Fully reproducible: same input → same output.
- **Spam `heuristics_v1`** — reasons: `contains_link`,
  `spam_phrase:<phrase>` (sub4sub, earn-money, free-followers, …),
  `duplicate_text` (same normalised text repeated in the set),
  `excessive_caps` (>70% caps over ≥10 letters), `repetitive_words`,
  `character_spam`. Spam is excluded from bands/themes/questions and
  counted in `spam_count`.

## TOP-QUESTIONS SEAM (for the Round 5 learning team)

```python
from app.comment_intel import top_questions_for_channel

questions = top_questions_for_channel(channel_rollup)   # or [video_rollups]
# -> [{"question_text": str, "video_id": str | None,
#      "comment_id": str, "like_count": int}, ...]
```

- Ranked by `like_count` desc, then the source comment's
  `reply_count` desc.
- `question_text` is the **verbatim question sentence from the
  viewer's public comment**, quoted for the channel owner's own
  review and stored exactly as extracted — never paraphrased. (In LLM
  mode, non-verbatim questions are dropped rather than stored.)
- The same items appear typed as `CommentRollup.top_questions`
  (`TopQuestion`) on both video and channel rollups.
- Engine entry points from the store:
  `CommentAnalysisEngine.analyse_stored_video(store, channel_id, video_id)`
  and `analyse_stored_channel(store, channel_id)` return
  `(rollup, persist_report)`.

## Persistence

- **Comments store (preferred):**
  `app.comment_intel.storage.persist_rollup_to_store(store, rollup)`
  writes through `CommentsStore.create_analysis` (provenance is a
  first-class column set: `engine`, `model_id`, `provenance_json`,
  full rollup in `rollup_json`) and files every spam flag via
  `create_moderation_flag(comment_id, 'spam', reason=…,
  suggested_action='review')`. `suggested_action` is advisory text
  only — flags never auto-execute; the engagement worker's moderation
  queue / a human decides (`pending → dismissed | actioned`). The
  returned report counts flags skipped (comment not stored / scope
  mismatch) instead of hiding them.
- **Raw connection fallback:** `persist_rollup_to_db(conn, rollup)`
  introspects an existing `comment_analyses` /
  `comment_moderation_flags` table and fills the columns it finds;
  it creates nothing and reports exactly what was written.
- **JSON file seam:** `CommentAnalysisStore(base_dir)` writes
  `<base>/<scope>-<key>/analysis.json` + an atomic `index.json`
  (default base: `<data_dir>/comment_intel/`), for offline tooling
  and tests. Latest analysis per scope/key wins.

## Tests & fixtures

`backend/tests/comment_intel/` — labelled synthetic sets
(enthusiastic fans, genuine questions in English + Roman Urdu,
complaints, obvious spam): band shares, verbatim top questions,
theme/topic sanity, deterministic reproducibility, LLM mode with a
mocked router (incl. ghost-id / fake-quote rejection), LLM failure →
labelled fallback and strict-mode typed error, channel aggregation,
the questions seam, JSON + raw-DB + real `CommentsStore` roundtrips.
