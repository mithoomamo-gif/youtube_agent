#!/usr/bin/env python
"""Runnable WPM calibration measurement (Round 4 TTS calibration).

Real edge-tts synthesis (keyless), ffprobe durations, no secrets printed.

Usage (from backend/):
    .venv/bin/python scripts/measure_tts_wpm.py \
        [--workdir ../.tmp-yta/tts-calib-audio] \
        [--raw ../outputs/round4/tts_proof/wpm_measurements.json] \
        [--canonical app/providers/tts/calibration_data.json] \
        [--extra-urdu-script]

Defaults write both contract outputs. ``--extra-urdu-script`` additionally
measures the ur-PK voices on the Urdu-script counterpart text; since the
R4-002 fix the canonical file is script-aware, so those rows land in each
ur-PK voice's ``scripts.urdu_script`` row (and become its adopted-default
top-level row) instead of being raw-run-only.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.providers.tts.calibration import (  # noqa: E402
    CANONICAL_PATH, DEFAULT_RATES_PCT, DEFAULT_VOICE_TEXTS, run_calibration,
    write_outputs)


async def _main(args: argparse.Namespace) -> int:
    voice_texts = dict(DEFAULT_VOICE_TEXTS)
    run = await run_calibration(
        voice_texts, DEFAULT_RATES_PCT, workdir=Path(args.workdir))
    if args.extra_urdu_script:
        extra = await run_calibration(
            {"ur-PK-UzmaNeural": "urdu_script_facts",
             "ur-PK-AsadNeural": "urdu_script_facts"},
            DEFAULT_RATES_PCT, workdir=Path(args.workdir) / "urdu_script")
        run.measurements.extend(extra.measurements)
        run.failures.extend(extra.failures)
    raw, canonical = write_outputs(run, Path(args.raw), Path(args.canonical),
                                   voice_texts)
    for m in run.measurements:
        print(f"{m.voice:18s} {m.rate:>5s} {m.text_id:18s} "
              f"words={m.word_count:3d} dur={m.duration_s:6.2f}s "
              f"wpm={m.wpm:7.2f} ({m.duration_source})")
    for f in run.failures:
        print(f"FAILED {f['voice']} {f['rate']}: {f['error']}")
    print(f"raw -> {raw}\ncanonical -> {canonical}")
    return 0 if run.measurements else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default="../.tmp-yta/tts-calib-audio")
    parser.add_argument(
        "--raw", default="../outputs/round4/tts_proof/wpm_measurements.json")
    parser.add_argument("--canonical", default=str(CANONICAL_PATH))
    parser.add_argument("--extra-urdu-script", action="store_true")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
