"""Encoder selection with a REAL test-encode probe (roadmap Part 6.7).

Priority order: NVENC (``h264_nvenc``) → QSV (``h264_qsv``) → AMF
(``h264_amf``) → libx264.

An encoder merely appearing in ``ffmpeg -encoders`` is NOT proof it works:
on a machine without an NVIDIA driver, ``h264_nvenc`` is still listed but
every encode fails (``Cannot load libcuda.so.1``).  The probe therefore
actually test-encodes a 2-second clip with each candidate, in priority
order, and the first candidate that produces a valid non-empty file wins.
The winner is cached (in-memory and, optionally, as a JSON file) so the
probe runs once per machine, not once per render.

Pipeline code never branches on the encoder name — it asks this module for
the argument list for a given quality tier.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from .contract import GOP_FRAMES
from ...ffmpeg_resolution import ffmpeg_executable

Tier = Literal["segment", "draft", "master"]

#: Probe duration — the roadmap specifies a short real encode (2 s here).
PROBE_SECONDS = 2

#: Render concurrency: 1 by default, hard maximum 2 (config value only —
#: the binding limit is filter-graph CPU + VRAM shared with other stages).
DEFAULT_RENDER_CONCURRENCY = 1
MAX_RENDER_CONCURRENCY = 2


def clamp_concurrency(requested: int) -> int:
    """Clamp a configured render-concurrency value into [1, 2]."""
    return max(1, min(MAX_RENDER_CONCURRENCY, int(requested)))


@dataclass
class EncoderCandidate:
    name: str  # ffmpeg encoder name, e.g. "h264_nvenc"
    label: str  # human label for UI/logs


CANDIDATES: list[EncoderCandidate] = [
    EncoderCandidate("h264_nvenc", "NVIDIA NVENC"),
    EncoderCandidate("h264_qsv", "Intel Quick Sync"),
    EncoderCandidate("h264_amf", "AMD AMF"),
    EncoderCandidate("libx264", "CPU libx264"),
]


def _quality_args(encoder: str, tier: Tier) -> list[str]:
    """Quality/preset arguments per encoder and tier (roadmap values)."""
    if encoder == "h264_nvenc":
        if tier == "draft":
            return ["-preset", "p1", "-tune", "hq", "-rc", "vbr",
                    "-cq", "28", "-b:v", "4M", "-maxrate", "6M", "-bufsize", "8M"]
        bitrate = ("24M", "36M", "48M") if tier == "master" else ("16M", "24M", "32M")
        return ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "19",
                "-b:v", bitrate[0], "-maxrate", bitrate[1], "-bufsize", bitrate[2]]
    if encoder == "h264_qsv":
        if tier == "draft":
            return ["-preset", "veryfast", "-global_quality", "28"]
        return ["-preset", "veryslow", "-global_quality", "20"]
    if encoder == "h264_amf":
        if tier == "draft":
            return ["-quality", "speed", "-rc", "cqp", "-qp_i", "28", "-qp_p", "30"]
        return ["-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22"]
    # libx264 — correctness fallback
    if tier == "draft":
        return ["-preset", "superfast", "-crf", "24"]
    return ["-preset", "veryfast", "-crf", "17"]


def _gop_args(encoder: str) -> list[str]:
    """Locked-GOP arguments; scene-cut locking differs per encoder."""
    args = ["-g", str(GOP_FRAMES)]
    if encoder == "libx264":
        args += ["-keyint_min", str(GOP_FRAMES), "-sc_threshold", "0"]
    elif encoder == "h264_nvenc":
        args += ["-no-scenecut", "1"]
    return args


def encoder_args(encoder: str, tier: Tier = "segment") -> list[str]:
    """Full ``-c:v ...`` argument list for an encoder + tier."""
    return ["-c:v", encoder, "-profile:v", "high",
            *_quality_args(encoder, tier), *_gop_args(encoder)]


@dataclass
class ProbeAttempt:
    encoder: str
    listed: bool  # present in `ffmpeg -encoders`
    encode_ok: bool  # a real test encode succeeded
    detail: str = ""


@dataclass
class EncoderChoice:
    """The persisted capability object the pipeline branches through."""

    encoder: str
    label: str
    attempts: list[ProbeAttempt] = field(default_factory=list)
    probe_ran_encode: bool = False  # proof the probe did real encodes

    def args(self, tier: Tier = "segment") -> list[str]:
        return encoder_args(self.encoder, tier)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "EncoderChoice | None":
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["attempts"] = [ProbeAttempt(**a) for a in data.get("attempts", [])]
            return cls(**data)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None


def _listed_encoders(ffmpeg: str = ffmpeg_executable()) -> set[str]:
    proc = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                          capture_output=True, text=True, timeout=60)
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def _test_encode(encoder: str, work_dir: Path, ffmpeg: str = ffmpeg_executable()) -> tuple[bool, str]:
    """Actually encode a 2-second clip.  Returns (ok, detail)."""
    out = work_dir / f"probe_{encoder}.mp4"
    cmd = [
        ffmpeg, "-y", "-v", "error",
        "-f", "lavfi", "-i",
        f"color=c=black:s=320x240:r=30:d={PROBE_SECONDS}",
        *encoder_args(encoder, "segment"),
        "-pix_fmt", "yuv420p", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    ok = proc.returncode == 0 and out.exists() and out.stat().st_size > 0
    detail = (proc.stderr or "").strip().splitlines()
    return ok, (detail[0][:200] if detail else f"exit={proc.returncode}")


# Module-level cache: probing is a per-machine, once-per-process operation.
_cached_choice: EncoderChoice | None = None


def probe_encoder(
    ffmpeg: str = ffmpeg_executable(),
    cache_file: Path | None = None,
    force: bool = False,
) -> EncoderChoice:
    """Probe candidates in priority order with REAL 2 s test encodes.

    Uses the JSON cache at ``cache_file`` when present (and ``force`` is
    False); otherwise probes and persists the winner there.
    """
    global _cached_choice
    if not force and _cached_choice is not None:
        return _cached_choice
    if not force and cache_file is not None:
        cached = EncoderChoice.load(cache_file)
        if cached is not None:
            _cached_choice = cached
            return cached

    listed = _listed_encoders(ffmpeg)
    attempts: list[ProbeAttempt] = []
    winner: EncoderCandidate | None = None

    with tempfile.TemporaryDirectory(prefix="encoder-probe-") as tmp:
        work = Path(tmp)
        for cand in CANDIDATES:
            is_listed = cand.name in listed
            if not is_listed:
                attempts.append(ProbeAttempt(cand.name, False, False,
                                             "not listed by ffmpeg -encoders"))
                continue
            ok, detail = _test_encode(cand.name, work, ffmpeg)
            attempts.append(ProbeAttempt(cand.name, True, ok, detail))
            if ok:
                winner = cand
                break

    if winner is None:  # libx264 is a build-time dependency; this is fatal.
        raise RuntimeError(
            "No usable H.264 encoder: even libx264 failed its test encode. "
            f"Attempts: {attempts}"
        )

    choice = EncoderChoice(
        encoder=winner.name,
        label=winner.label,
        attempts=attempts,
        probe_ran_encode=any(a.listed for a in attempts),
    )
    if cache_file is not None:
        choice.save(cache_file)
    _cached_choice = choice
    return choice


def reset_probe_cache() -> None:
    """Test hook: forget the in-memory cached encoder choice."""
    global _cached_choice
    _cached_choice = None
