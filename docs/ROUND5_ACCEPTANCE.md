# Round 5 (Growth + Learning) — Verification & Acceptance Summary

**Overall verdict: CONDITIONAL PASS.**

Round 5's features are real, working, and — consistently with this
project's honesty bar — careful never to fabricate data. The conditions are
two HIGH integration defects (R5-001, R5-002), one MEDIUM (R5-005), one
LOW–MEDIUM gap (R5-004), and one carry-over that did not land (styled proof
v2). **Everything Round 5 is uncommitted working-tree state on top of HEAD
`aaabab4`** (Style Lab); judged on the commit alone, all of Round 5 would be
NOT-LANDED. A short post-commit re-verification is recommended.

**Verification timeline note:** this team was dispatched before the feature
teams landed. The tree was clean at `aaabab4` at 06:32 UTC; the five feature
teams landed live between ~06:33 and ~08:20 UTC while verification ran
(through one VM reboot and one documented EPERM write-fault window). All
verdicts below are against the final settled working tree; every mid-flight
snapshot verdict was re-checked against it (notably Comments, whose first
pass correctly reported NOT-LANDED pre-landing and was fully re-verified).

## Regression vs baselines

| Suite | Baseline (R4/Style Lab settled) | Round 5 settled tree | Verdict |
|---|---|---|---|
| Backend | 1698 passed / 8 skipped (1706) | **2160 passed / 4 failed / 8 skipped / 1 xpassed / 8 errors (2181 collected, +475 R5 tests)** | 3 of the failures + the calibration migration test are CODE, all rooted in R5-001; the rest proven ENVIRONMENTAL (see below) |
| Root | 44 passed / 1 skipped | 39 passed / 1 failed / 4 errors / 1 skipped under load ~30; **all 5 non-passing proven ENVIRONMENTAL** on standalone re-runs (sidecar-boot contention timeouts) → effectively 44/1 | At baseline |
| UI | typecheck + build green | **typecheck exit 0, build exit 0** on the final tree (transient mid-landing cross-file errors were fixed by their owners) | PASS |

Environmental proof: an earlier full run showed 331 non-passing results;
319 vanished on the mandated quiet re-run, with verbatim
`PermissionError: [Errno 1]` on tmp writes and sqlite `disk I/O error` in
the bodies — the documented VM EPERM fault, disk at 1% used. The known
flakes behaved as documented (caplog-ordering failure green standalone;
`test_ws` heartbeat green with uptime > 1030s).

## Acceptance checklist

### (a) Analytics — CONDITIONAL PASS
- Not-connected performs **zero HTTP**: PASS, proven at client (23 tests),
  sync (15) and E2E level — recorded `not_connected` reason, zero writes,
  no API-key path by design.
- Mocked-transport sync stores **idempotent snapshots**: PASS — re-sync
  re-issues HTTP (6→12 requests) but row ids/counts unchanged
  (`UNIQUE(channel_id, youtube_video_id, snapshot_kind, capture_day)`).
- Retention→section mapping on a **real project pack**: PASS — Round 3
  facts pack, 5 sections from real artefacts, cliff at 1:40 mapped into
  Chapter 3. Caveat: the retention curve fed in is synthetic-labelled; no
  real audience data exists yet (no connected channel).
- Freshness stamps in read models: PASS for overview/sync-status
  (`last_synced_at`, per-snapshot `captured_at`, YPP `as_of`) — but see
  R5-002: the per-video read path is broken in production creation order,
  which is what makes this item conditional.
- Battery: 112 passed, 1 xfail (the xfail *is* R5-002, documented by the
  feature team itself).

### (b) Comments — PASS with one PARTIAL
- Sync idempotent on fixtures: PASS.
- Theme/question extraction provenance: PASS — engine/model/method labels on
  every rollup; deterministic fallback always labelled with its reason;
  mixed-engine aggregates downgraded, never upgraded. No silent stub.
- Draft lifecycle: **PARTIAL FAIL** — create → approve/reject proven with
  terminal-state guards; **no edit capability, no distinct discard state**
  (R5-004).
- **No automatic posting path: PASS (highest-severity check)** — no posting
  implementation exists; the only `comments.insert` occurrence is the quota
  cost constant (`providers/youtube/quota.py:59`); approval is a purely
  local status update; nothing outside the comments router imports the
  feature, so scheduler/runner/pipeline cannot reach it.
- Battery: 123 passed, 0 failed (settled re-verification).

### (c) Learning — PASS with caveats (R5-005)
- Rules evidence-counted + versioned: PASS (schema DDL identical in runtime
  and migration 0005; supersede chains preserve old rows).
- Min-evidence gate: PASS via the engine (n≥3; n=2 stays candidate/thin) —
  caveats in R5-005 (label threshold 5 vs activation 3; store-level
  activation ungated).
- Shrinkage math: PASS, **exact** — three hand-computed examples match the
  implementation to 6 decimal places (k=4 topic_affinity, k=5 other kinds).
- Scope isolation (niche AND channel): PASS — no unscoped read path exists;
  cross-scope reads return None, writes raise; contamination tests green.
- Publish-window eligibility: PASS — below ≥10 published videos AND ≥28
  days of analytics it returns `not_enough_data` with the counts, never a
  fabricated window; suggestions stored only as candidates; never touches
  schedules.
- Calibration error computed + visible: PASS — bias/MAPE/coverage per
  (channel, niche, metric) at D+7/D+28, hand-verified; surfaced via
  `GET /{channel_id}/learning/calibration` and the Agent-31 report section;
  below n=3 it reports `insufficient_evidence`, not numbers.
- Battery: 119 passed / 1 failed (the failure is R5-001-linked).

### (d) Reporting / UI — PASS
- Receipt reconciliation: **PASS, exact** — both receipt engines (W1
  `app.reporting`, W2 `app.reports`) reconcile to the cent/unit against
  independently recomputed ledger arithmetic (total 125,000 micros; quota
  1600+1+1 = 1602); three pre-existing per-video production reports also
  reconcile exactly. One honesty bug found and fixed mid-round (R5-003).
- Analytics screen: PASS — real freshness stamps ("Analytics updated …" /
  "not synced yet"), honest empty states everywhere, no placeholder
  numbers; the old hard-coded stub screen is gone.
- YPP widget: PASS — targets explicitly labelled (1,000 subs; 4,000 watch
  hours current bar; separate labelled 8,000-hour panel for applications
  from 1 Feb 2027); qualification built only from `qualifiedWatchHours`;
  when unavailable it shows "No figure is shown rather than an estimate" —
  it never fabricates qualification.

### (e) Quality carry-overs — 2 PASS, 1 NOT-LANDED
- Master-tier render: **PASS with caveats** — ffprobe-verified by this team:
  `outputs/round5/master_proof/final_master_1080p.mp4`, h264 1920×1080,
  76.5s (the only 1080p output in the tree); the app's own QC on the master
  PASS at −14.0 LUFS; tier named in `evidence.json` and in the production
  report format. Caveats: direct `render_final` re-render of the Round 4
  facts project (full pipeline + per-channel tier setting not exercised,
  no production `report.md` from this run); `master_1440p_upscale` tier
  unproven.
- Fair subtitle metric (R4-005): **PASS** — production QC now emits
  `script_match_ratio_fair` (report-only; the strict gate is unchanged);
  this team independently recomputed the Round 3 values and matched the
  reports exactly: facts 0.1279, history 0.0339, cricket 0.0558
  (vs strict 0.0066 / 0.0000 / 0.0041).
- Styled proof v2 (SL-006): **NOT-LANDED** — a v2 run was mid-production at
  verification time (real-LLM stage-bridge pattern, flash-lite tier
  substitution disclosed, brightness filter for the dark-clip problem), but
  there is no completed v2 proof: no cost/provenance record (no ledger
  cost > 0 on file) and no gate outcome. The only completed styled proof
  remains v1 (deterministic stub, $0.00, Gate B FAIL as reported).

### Credential hygiene — PASS
`.env` untracked, ignored, mode 600, containing only the YouTube OAuth pair;
value-level sweep found those values in zero other files; full-tree pattern
sweep (Gemini `AQ.`, `AIza`, `sk-`, Pixabay-style keys, Bearer tokens) found
only obvious test fakes. No real key material in the tree or outputs.

## Bug list (detail in docs/ROUND5_BUGS.md)

| ID | Severity | Summary | Status |
|---|---|---|---|
| R5-001 | HIGH | Duplicate Alembic revision `0008` (calibration_log + channel_quality_tier): two heads, `alembic upgrade head` fails; breaks 3 baseline migration tests + the calibration migration test | OPEN — renumber one file to 0009 |
| R5-002 | HIGH | Two competing `channel_metric_snapshots` schemas; per-video analytics reads broken in production (sync-first) creation order | OPEN — unify DDL, remove the xfail |
| R5-003 | HIGH at discovery | W2 receipts presented simulated publishes as real publications (`simulated:` prefix unrecognised) | FIXED by the team mid-round; fix is uncommitted — confirm it lands in the commit |
| R5-004 | LOW–MEDIUM | Comment drafts cannot be edited; no distinct discard state | OPEN |
| R5-005 | MEDIUM | Learning: activation threshold (3) ≠ thin-evidence label (5); store-level `activate_rule` ungated | OPEN |

## What remains genuinely open

1. R5-001 and R5-002 (both HIGH) — integration fixes for the feature teams;
   R5-001 also gates a clean full-suite run.
2. R5-005 threshold/gate hardening; R5-004 draft edit/discard.
3. Styled proof v2: re-verify once the in-flight run finishes (needs
   ledger cost > 0, real-model provenance, gate outcome as-is).
4. Master-tier proof through the full produce pipeline with a production
   `report.md`, and the 1440p-upscale tier.
5. Minor: analytics idempotency day definition differs by layer (UTC store
   vs Pacific sync) — reconcile with R5-002.
6. **Commit Round 5** (nothing is committed) and re-run this acceptance
   briefly against the committed tree, confirming in particular that the
   R5-003 fix and the eventual R5-001/R5-002 fixes are in.
