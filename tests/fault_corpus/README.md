# QC fault-injection corpus (Section 17, L6)

Deliberately broken artefacts with known labels, used to test the QC gates
themselves. A quality gate never seen to fail is assumed broken — this
corpus is how the gates prove they can still say **NO**.

`cases.json` is the seed enumeration (metadata/labelling only; the broken
media artefacts themselves are produced by the L6 harness, hermetically).
Every case is a **must-fail**: `expected_verdict` is always `FAIL`.

## Recall bars (acceptance, Section 17 L6)

- **100% recall on safety / policy / copyright must-fails** (unlicensed
  asset, missing licence-ledger entry, reused script, missing disclaimer,
  misleading thumbnail, ...) — one miss is a release blocker.
- **>=98% recall on technical must-fails** (black/freeze/silence,
  loudness/format/duration, subtitle offset, ...).
- **<=2% false-FAIL on the clean golden set** — a QC that blocks good
  videos breaks "fully automatic" just as surely as one that passes bad ones.

Related per-agent bars: fact-check catches >=90% of planted errors
(golden `fact_check` dataset); the upload agent must produce **zero
duplicate uploads, ever** (QC-FAULT-011); the similarity gate blocks
**>=19/20** of a synthetic templated series (QC-FAULT-003).

## Size targets

- MVP (Phase 1): corpus **>=15 cases** — this seed meets that bar.
- v1 (Phase 2): corpus grows to **>=25 cases**, adding the remaining
  Section 17 L6 families: wrong aspect / non-yuv420p / missing +faststart,
  truncated render, subtitle cue >42 chars / >7s / spelling corruption,
  watermarked stock asset, music-over-voice, still scene without Ken Burns
  motion, thumbnail illegible at 10% downscale, and the -8 LUFS loudness
  variant.

`test_fault_corpus.py` validates that the corpus parses, ids are unique,
and every case carries an expected FAIL verdict.
