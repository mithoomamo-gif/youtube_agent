# Hermetic fixtures (Section 17, L4)

Small, purpose-built, **frozen** fixture projects for the hermetic pipeline
suite. Each fixture is `fixture.json` (manifest) + tiny original seed inputs
(`topic.txt` / `brief.md` / `script_excerpt.md`). No external downloads, no
real media — media is represented by `MEDIA_PLACEHOLDER.md`.

| Fixture | Status | Niche | Language | Target |
|---|---|---|---|---|
| F-EN-BASIC | full | facts (built-in) | English | ~60s |
| F-UR-BASIC | full | islamic stories (built-in) | Urdu (Arabic script) | ~60s |
| F-OTHER-CUSTOM | full | cricket analysis (**Other**/custom) | English | ~60s |
| F-MIX-CODESWITCH | stub manifest | tech (built-in) | Roman Urdu + English code-switch | ~90s |
| F-NOSTOCK | stub manifest | facts (built-in), obscure topic | English | ~45s |

Not yet seeded (owned by the L4 suite build-out, per Section 17): F-LONG
(10-min, nightly), F-EDGE-TEXT, and the F-FAIL-* family (one injected failure
per stage: LLM 429, TTS timeout mid-chapter, corrupt stock download, FFmpeg
non-zero exit, quota exhausted, disk full).

Every manifest carries the Section 17 L4 assertions: artefact completeness
with 100% licence-ledger coverage, DAG/gate ordering (gates 32/33/34 PASS
before upload is callable, out-of-order upload refused), upload idempotency
(exactly one `videos.insert` per idempotency key), quota-ledger math, no
re-execution of completed paid steps on resume, and the hermetic
determinism boundary.
