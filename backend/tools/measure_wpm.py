#!/usr/bin/env python
"""CLI: measure delivered TTS WPM per voice x rate (Round 4 calibration).

Thin wrapper over :mod:`app.providers.tts.calibration` — all measurement
logic (synthesis, ffprobe duration, WPM math, JSON writing) lives there;
this file only parses arguments and chooses real vs mock synthesis.

Real mode (default; keyless edge-tts, network required):

    .venv/bin/python tools/measure_wpm.py \
        [--out ../outputs/round4/tts_proof/wpm_measurements.json] \
        [--canonical app/providers/tts/calibration_data.json] \
        [--workdir ../.tmp-yta/tts-calib-audio] [--extra-urdu-script]

Mock mode (NO network; deterministic fake synthesizer through the SAME
``run_calibration`` code path — durations come from the fake's
WordBoundary-style return because ffprobe rejects the dummy audio, so
rows are honestly marked ``duration_source="word_boundary"``):

    .venv/bin/python tools/measure_wpm.py --mock [--out <path>]

Mock mode never overwrites the real proof files by default: its raw
output defaults to ``../.tmp-yta/mock_wpm_measurements.json`` and the
canonical file is written only with ``--write-canonical``.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.providers.tts.calibration import (  # noqa: E402
    CANONICAL_PATH, DEFAULT_RATES_PCT, DEFAULT_VOICE_TEXTS,
    count_words, run_calibration, write_json, build_calibration_data)

_URDU_SCRIPT_RE = re.compile(r"[؀-ۿ]")

#: Nominal 0%-rate WPM the deterministic mock pretends each family
#: delivers (close to the real Round 4 measurements, for shape only).
_MOCK_BASE_WPM = {"ur_roman": 283.0, "ur_script": 177.0, "en": 180.0}

MOCK_METHOD = (
    "MOCK run (tools/measure_wpm.py --mock): deterministic fake "
    "synthesizer through calibration.run_calibration; no network, no "
    "real audio; durations are the fake's boundary durations, NOT "
    "ffprobe measurements. Do not consume as calibration data."
)


async def _mock_synthesize(voice_id: str, rate_pct: float,
                           text: str) -> tuple[bytes, Optional[float]]:
    """Deterministic fake: duration = words / (base_wpm * rate_factor)."""
    if _URDU_SCRIPT_RE.search(text):
        base = _MOCK_BASE_WPM["ur_script"]
    elif voice_id.startswith("ur-"):
        base = _MOCK_BASE_WPM["ur_roman"]
    else:
        base = _MOCK_BASE_WPM["en"]
    wpm = base * (1.0 + float(rate_pct) / 100.0)
    duration_s = count_words(text) / wpm * 60.0
    return b"MOCK-AUDIO-NOT-REAL", duration_s


async def _run(args: argparse.Namespace) -> int:
    voice_texts = dict(DEFAULT_VOICE_TEXTS)
    synthesizer = _mock_synthesize if args.mock else None
    run = await run_calibration(
        voice_texts, DEFAULT_RATES_PCT, workdir=Path(args.workdir),
        synthesizer=synthesizer)
    if args.extra_urdu_script and not args.mock:
        extra = await run_calibration(
            {"ur-PK-UzmaNeural": "urdu_script_facts",
             "ur-PK-AsadNeural": "urdu_script_facts"},
            DEFAULT_RATES_PCT, workdir=Path(args.workdir) / "urdu_script")
        run.measurements.extend(extra.measurements)
        run.failures.extend(extra.failures)
    if args.mock:
        run.method = MOCK_METHOD

    raw_path = write_json(run.to_raw_dict(), Path(args.out))
    print(f"raw ({len(run.measurements)} measurements, "
          f"{len(run.failures)} failures) -> {raw_path}")
    for m in run.measurements:
        print(f"{m.voice:18s} {m.rate:>5s} {m.text_id:18s} "
              f"words={m.word_count:3d} dur={m.duration_s:6.2f}s "
              f"wpm={m.wpm:7.2f} ({m.duration_source})")
    for f in run.failures:
        print(f"FAILED {f['voice']} {f['rate']}: {f['error']}")

    if not args.mock or args.write_canonical:
        canonical_path = write_json(
            build_calibration_data(run, voice_texts), Path(args.canonical))
        print(f"canonical -> {canonical_path}")
    else:
        print("canonical NOT written (mock mode; pass --write-canonical "
              "to override — not recommended with fake data)")
    return 0 if run.measurements else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true",
                        help="deterministic fake synthesizer, no network")
    parser.add_argument("--out", default=None,
                        help="raw measurements JSON path (default: real "
                             "proof path, or .tmp-yta mock path in --mock)")
    parser.add_argument("--canonical", default=str(CANONICAL_PATH))
    parser.add_argument("--write-canonical", action="store_true",
                        help="in --mock mode only: also write canonical")
    parser.add_argument("--workdir", default="../.tmp-yta/tts-calib-audio")
    parser.add_argument("--extra-urdu-script", action="store_true",
                        help="real mode: also measure ur-PK voices on the "
                             "Urdu-script sample text (raw run only)")
    ns = parser.parse_args()
    if ns.out is None:
        ns.out = ("../.tmp-yta/mock_wpm_measurements.json" if ns.mock
                  else "../outputs/round4/tts_proof/wpm_measurements.json")
    raise SystemExit(asyncio.run(_run(ns)))
