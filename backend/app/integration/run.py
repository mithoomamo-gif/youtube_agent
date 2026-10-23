"""CLI for the ROUND 2 integration pipeline.

Usage::

    python -m app.integration.run --niche facts \\
        --topic "Samundar ki gehrai ke raaz" --minutes 1

    python -m app.integration.run --niche other \\
        --custom-niche "cricket analysis" \\
        --niche-idea "matches ki kahani aur stats Urdu mein" \\
        --topic "1992 World Cup final ki kahani" --minutes 1

Prints the :class:`ProductionResult` as a JSON summary on stdout and
exits 0 on success (a completed run -- including a gate-blocked one --
is a result, not a CLI failure).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

__all__ = ["build_parser", "main", "result_to_dict"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.integration.run",
        description="Produce one YouTube video end-to-end (Round 2 integration).",
    )
    parser.add_argument("--niche", default="facts",
                        help="Built-in niche id (e.g. facts) or 'other'.")
    parser.add_argument("--custom-niche", dest="custom_niche", default=None,
                        help="Custom niche name when --niche other.")
    parser.add_argument("--niche-idea", dest="niche_idea", default=None,
                        help="Free-text niche idea when --niche other.")
    parser.add_argument("--topic", required=True, help="Video topic.")
    parser.add_argument("--minutes", type=float, default=1.0,
                        help="Target video length in minutes (default 1).")
    parser.add_argument("--language", default="ur",
                        help="Narration language code (default ur).")
    parser.add_argument("--voice-id", dest="voice_id", default=None,
                        help="TTS voice id; default lets the voice stage choose.")
    parser.add_argument("--channel-id", dest="channel_id",
                        default="local-test-channel",
                        help="Channel id for the project.")
    parser.add_argument("--publish-mode", dest="publish_mode",
                        default="review_first",
                        choices=["review_first", "auto"],
                        help="Publish mode (default review_first).")
    parser.add_argument("--seed", type=int, default=42,
                        help="Deterministic seed (default 42).")
    parser.add_argument("--base-dir", dest="base_dir", default=None,
                        help="Output base directory. Default: "
                             "<YTA_DATA_DIR>/projects/cli_<niche> when "
                             "YTA_DATA_DIR is set, else "
                             "<repo>/outputs/round2/cli_<niche>.")
    return parser


def spec_from_args(args: argparse.Namespace) -> Any:
    """Build a ProjectSpec from parsed CLI args (kept separate for tests)."""
    from app.integration.models import ProjectSpec

    return ProjectSpec(
        niche=args.niche,
        custom_niche_name=getattr(args, "custom_niche", None),
        niche_idea=getattr(args, "niche_idea", None),
        topic=args.topic,
        minutes=args.minutes,
        language=getattr(args, "language", "ur") or "ur",
        voice_id=getattr(args, "voice_id", None),
        channel_id=getattr(args, "channel_id", "local-test-channel")
        or "local-test-channel",
        publish_mode=getattr(args, "publish_mode", "review_first")
        or "review_first",
        seed=getattr(args, "seed", 42),
    )


def result_to_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        return dict(result.model_dump(mode="json"))
    if isinstance(result, dict):
        return dict(result)
    return dict(vars(result))


def _default_base_dir(niche: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in niche.lower())
    # Round 6 data-dir contract: with YTA_DATA_DIR explicitly set
    # (packaged/frozen runs), CLI productions root under the data
    # root's projects area like every other production; dev default
    # (override unset) is the historical repo outputs dir, unchanged.
    from app.data_dir import data_dir_layout, explicit_data_dir

    explicit = explicit_data_dir()
    if explicit is not None:
        return data_dir_layout(explicit).projects_dir / f"cli_{safe}"
    # backend/app/integration/run.py -> repo root is parents[3] of backend/
    backend_dir = Path(__file__).resolve().parents[2]
    repo_root = backend_dir.parent
    return repo_root / "outputs" / "round2" / f"cli_{safe}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    from app.integration.produce import produce_video

    spec = spec_from_args(args)
    base_dir = Path(args.base_dir) if args.base_dir else _default_base_dir(args.niche)
    result = produce_video(spec, base_dir)
    print(json.dumps(result_to_dict(result), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
