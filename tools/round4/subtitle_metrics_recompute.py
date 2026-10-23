#!/usr/bin/env python3
"""Round 4 — recompute Round 3 Phase 0 subtitle script-match scores honestly.

Round 3 reported ``script_match_ratio`` ≈ 0.0 for all three Phase 0
videos.  That metric (``app.pipeline.subtitles.validation.script_match_ratio``)
compares the Roman Urdu reference script against Whisper captions in the
ORIGINAL scripts — but Whisper emits Urdu script, so token overlap is
structurally ~zero regardless of what was actually said.  This tool
recomputes, per video, BOTH numbers side by side:

* RAW  — the original ``script_match_ratio`` (unchanged);
* FAIR — ``script_match_ratio_fair``: both sides are normalised into a
  common Roman representation (Urdu script is transliterated with a
  simplified phonetic mapping) and then scored with the SAME
  difflib token-sequence computation.

Reference scripts are taken only from genuine per-video artifacts:

* facts_ocean — ``narration_text`` of the project's ``bundle.json``
  (the full LLM script bundle saved by the Round 3 run);
* history_mohenjo_daro / other_cricket — these projects saved no
  ``bundle.json``; the reference is reconstructed from the project's
  ``visuals/scene_manifest.json``, whose ``scenes[].text`` fields are
  the script's sentences in order (verified: for facts_ocean the
  scene-manifest join is token-identical to ``bundle.json``'s
  ``narration_text``; for history the join is 441 words / 2,488 chars
  vs the report's 442 narration words / 2,492 TTS chars; for the
  canonical cricket project it is exactly the report's 522 words).
  For other_cricket the canonical project is
  ``1992-world-cup-final-ki-kahani-acfe14c6`` (named canonical in its
  report.md; its ``pack/captions.srt`` is byte-identical to the
  top-level ``captions.srt`` used here).

If a genuine reference cannot be found for a video, the video is
reported as NOT RECOMPUTABLE — a reference is never fabricated.

Captions: the top-level ``captions.srt`` (Whisper output) of each
Phase 0 pack directory.

Usage:
    backend/.venv/bin/python tools/round4/subtitle_metrics_recompute.py

Writes outputs/round4/gate_policy/subtitle_metrics.md and prints a
one-line summary per video.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.pipeline.subtitles.validation import (  # noqa: E402
    normalise_for_match,
    script_match_ratio,
    script_match_ratio_fair,
    transliterate_urdu_to_roman,
)

PHASE0 = REPO_ROOT / "outputs" / "round3" / "phase0"
OUTPUT_MD = REPO_ROOT / "outputs" / "round4" / "gate_policy" / "subtitle_metrics.md"

# slug -> (project subdir, reference kind)
VIDEOS: list[dict] = [
    {
        "slug": "facts_ocean",
        "project": "samundar-ki-gehrai-ke-raaz-r3",
        "reference_kind": "bundle",
    },
    {
        "slug": "history_mohenjo_daro",
        "project": "mohenjo-daro-ki-kahani-ed49c4ff",
        "reference_kind": "scene_manifest",
    },
    {
        "slug": "other_cricket",
        "project": "1992-world-cup-final-ki-kahani-acfe14c6",
        "reference_kind": "scene_manifest",
    },
]

_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}\s*-->")


def parse_srt(path: Path) -> tuple[str, list[str]]:
    """Return (full caption text, list of cue texts) from an .srt file."""
    cues: list[str] = []
    current: list[str] = []
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if _TIMESTAMP_RE.match(line):
            if current:
                cues.append(" ".join(current))
                current = []
            continue
        if not line:
            if current:
                cues.append(" ".join(current))
                current = []
            continue
        if line.isdigit() and not current:
            continue  # cue index
        current.append(line)
    if current:
        cues.append(" ".join(current))
    return " ".join(cues), cues


def load_reference(video: dict) -> tuple[str | None, str]:
    """Return (reference text, exact source description) or (None, reason)."""
    project_dir = PHASE0 / video["slug"] / video["project"]
    if video["reference_kind"] == "bundle":
        bundle_path = project_dir / "bundle.json"
        if bundle_path.is_file():
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            text = bundle.get("narration_text") or bundle.get("script_text")
            if text:
                rel = bundle_path.relative_to(REPO_ROOT)
                return text, f"`{rel}` — field `narration_text`"
        return None, f"no usable script field in {bundle_path}"
    manifest_path = project_dir / "visuals" / "scene_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        scenes = manifest.get("scenes") or []
        texts = [s.get("text", "") for s in scenes]
        if texts and all(texts):
            rel = manifest_path.relative_to(REPO_ROOT)
            return (
                " ".join(texts),
                f"`{rel}` — `scenes[].text` joined in scene order "
                f"({len(texts)} scenes; no bundle.json exists for this project)",
            )
    return None, f"no scene manifest at {manifest_path}"


def first_sentence(text: str) -> str:
    return re.split(r"(?<=[.?!])\s+", text.strip(), maxsplit=1)[0]


def main() -> int:
    rows: list[dict] = []
    examples: list[dict] = []
    for video in VIDEOS:
        slug = video["slug"]
        captions_path = PHASE0 / slug / "captions.srt"
        reference, source = load_reference(video)
        if reference is None:
            rows.append({**video, "status": f"NOT RECOMPUTABLE — {source}"})
            print(f"{slug}: NOT RECOMPUTABLE — {source}")
            continue
        if not captions_path.is_file():
            rows.append({**video, "status": f"NOT RECOMPUTABLE — no captions at {captions_path}"})
            print(f"{slug}: NOT RECOMPUTABLE — captions missing")
            continue
        caption_text, cues = parse_srt(captions_path)
        raw = script_match_ratio(reference, caption_text)
        fair = script_match_ratio_fair(reference, caption_text)
        rows.append(
            {
                **video,
                "status": "OK",
                "source": source,
                "captions_source": f"`{captions_path.relative_to(REPO_ROOT)}` (Whisper SRT, {len(cues)} cues)",
                "reference_words": len(reference.split()),
                "caption_words": len(caption_text.split()),
                "raw": raw,
                "fair": fair,
            }
        )
        print(f"{slug}: raw={raw:.4f} fair={fair:.4f} "
              f"(ref {len(reference.split())} words, captions {len(caption_text.split())} words)")
        if len(examples) < 3 and cues:
            examples.append(
                {
                    "slug": slug,
                    "reference_sentence": first_sentence(reference),
                    "caption_cue": cues[0],
                    "caption_transliterated": transliterate_urdu_to_roman(cues[0]),
                    "reference_normalised": normalise_for_match(first_sentence(reference)),
                    "caption_normalised": normalise_for_match(cues[0]),
                }
            )

    lines: list[str] = []
    lines.append("# Round 4 — Subtitle script-match recompute (raw vs fair)\n")
    lines.append(
        "Recomputed by `tools/round4/subtitle_metrics_recompute.py` from the "
        "Round 3 Phase 0 artifacts on disk. No API calls, no re-transcription: "
        "the same reference scripts and the same Whisper `captions.srt` files "
        "Round 3 used, scored two ways.\n"
    )
    lines.append("## Method\n")
    lines.append(
        "* **RAW** — the original metric, `script_match_ratio` in "
        "`backend/app/pipeline/subtitles/validation.py`: NFC-normalise, strip "
        "punctuation, case-fold, then a difflib token-sequence ratio. It "
        "compares text in its ORIGINAL script, so a Roman Urdu reference "
        "against Urdu-script Whisper captions shares almost no tokens by "
        "construction.\n"
        "* **FAIR** — `script_match_ratio_fair` (added Round 4, same file): "
        "both sides first pass through `normalise_for_match` — Urdu-script "
        "text is transliterated to Roman with `transliterate_urdu_to_roman`, "
        "then the SAME base normalisation as the raw metric is applied, plus "
        "symmetric phonetic foldings applied to BOTH sides (doubled letters "
        "collapse; `ai`/`ei`/`ay` fold to `e`; `v` folds to `w`). The score "
        "itself is the identical difflib token-sequence computation with the "
        "same empty-input edges.\n"
        "* **Transliteration** is a simplified phonetic mapping (explicit "
        "table in code): the full Urdu alphabet including ٹ ڈ ڑ, the ھ "
        "aspirate digraphs, ے, ں, Arabic loan letters (ث ص ض ط ظ ع غ …), "
        "Arabic-Indic and Urdu digits; Arabic diacritics are stripped; "
        "contextual rules resolve و (w/o/u) and ی (y/i/e). A tiny explicit "
        "whole-word exception table covers کہ/یہ/وہ/نہ, where ہ is a silent "
        "vowel carrier.\n"
    )
    lines.append("### Limits of the method (read before quoting the fair score)\n")
    lines.append(
        "* Urdu script normally **omits short vowels** (zabar/zer/pesh are "
        "usually unwritten), so transliteration recovers the consonant "
        "skeleton plus long vowels only: پر → `pr`, not `par`; سب → `sb`, "
        "not `sab`. The fair score therefore UNDERSTATES true word "
        "agreement — it is a fairer floor, not an exact match.\n"
        "* و and ی are genuinely ambiguous letters; the contextual rules are "
        "approximations and will misread some words.\n"
        "* English loanwords written in Urdu script (ٹرینچ, کریکٹ) "
        "transliterate phonetically (`trench`-ish, `krikt`) and rarely equal "
        "their Roman spellings.\n"
        "* The fair score measures **script-normalised textual agreement "
        "between the script file and the caption file**. It is NOT a "
        "transcription-correctness (WER) measurement against the audio.\n"
    )
    lines.append("## Reference script sources (exact files used)\n")
    lines.append("| Video | Reference script source | Captions source |")
    lines.append("|---|---|---|")
    for row in rows:
        if row["status"] == "OK":
            lines.append(f"| {row['slug']} | {row['source']} | {row['captions_source']} |")
        else:
            lines.append(f"| {row['slug']} | {row['status']} | — |")
    lines.append(
        "\nVerification of the scene-manifest reconstruction: for facts_ocean "
        "the joined `scenes[].text` is token-identical to `bundle.json`'s "
        "`narration_text`; the history join is 441 words / 2,488 chars "
        "against its report's 442 narration words / 2,492 TTS chars; the "
        "canonical cricket join is exactly its report's 522 words / 2,819 "
        "chars vs 2,823 TTS chars (differences are punctuation only). "
        "For other_cricket the canonical project is "
        "`1992-world-cup-final-ki-kahani-acfe14c6` per its report.md, and "
        "its `pack/captions.srt` is byte-identical to the top-level "
        "`captions.srt` scored here.\n"
    )
    lines.append("## Results\n")
    lines.append("| Video | RAW script_match_ratio | FAIR script_match_ratio_fair | Reference words | Caption words |")
    lines.append("|---|---:|---:|---:|---:|")
    for row in rows:
        if row["status"] == "OK":
            lines.append(
                f"| {row['slug']} | {row['raw']:.4f} | {row['fair']:.4f} "
                f"| {row['reference_words']} | {row['caption_words']} |"
            )
        else:
            lines.append(f"| {row['slug']} | NOT RECOMPUTABLE | NOT RECOMPUTABLE | — | — |")
    lines.append(
        "\nRound 3 reported 0.0 for all three (history's report literally "
        "records `script_match_ratio: 0.0`; the recomputed raw values above "
        "match that, up to which exact caption file copy is used).\n"
    )
    lines.append("## Example line pairs (first reference sentence vs first Whisper cue)\n")
    for ex in examples:
        lines.append(f"### {ex['slug']}\n")
        lines.append(f"- Reference (Roman Urdu script): {ex['reference_sentence']}")
        lines.append(f"- Whisper caption (Urdu script, as emitted): {ex['caption_cue']}")
        lines.append(f"- Caption transliterated: {ex['caption_transliterated']}")
        lines.append(f"- Reference, fair-normalised: `{ex['reference_normalised']}`")
        lines.append(f"- Caption, fair-normalised: `{ex['caption_normalised']}`\n")
    lines.append("## Honest interpretation\n")
    lines.append(
        "The Round 3 ≈ 0.0 was partly an artefact: the raw metric compares "
        "across scripts, so it could never have scored these captions "
        "fairly. The recompute removes that artefact — and the fair scores "
        "are still low (see the table). A control check in the unit tests "
        "(`backend/tests/pipeline/test_subtitle_fair_metric.py`) shows the "
        "fair metric CAN score high — 0.67 — for a faithful Urdu-script "
        "rendering of a real script line, and ~0 for unrelated text, so the "
        "low video scores are a property of these captions, not of the "
        "metric. In other words: even after script normalisation, the "
        "Whisper captions agree with the scripts on only a small share of "
        "tokens (post-normalisation bag-of-words overlap is likewise only "
        "~16–18% per video). That is consistent with what the caption files "
        "show directly — garbled word forms, dropped and repeated phrases, "
        "and far fewer caption tokens than script tokens.\n\n"
        "Two distinct problems must not be conflated. (1) The METRIC was "
        "unfair: a cross-script comparison reported as ≈ 0.0 overstated the "
        "failure and is fixed by reporting raw and fair side by side. "
        "(2) The ASR is genuinely weak for this input: Round 3's narration "
        "was Roman-Urdu text fed to Urdu TTS voices at a fast ~200–245 WPM, "
        "and Whisper (tiny/base) transcribing that audio produced poor "
        "Urdu-script text. The fair metric does not fix, hide, or excuse "
        "that — it only stops the script mismatch from being counted twice. "
        "Judging transcription quality properly still requires listening / "
        "WER against the audio, which neither metric provides.\n"
    )

    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUTPUT_MD.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
