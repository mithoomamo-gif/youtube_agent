"""Acceptance test E — Round 2 E2E outputs validation.

Validates the two *real* 1-minute videos produced by the Round 2 E2E
integration team (``backend/app/integration/``) into::

    <repo>/outputs/round2/

Expected outputs
----------------
* one video for the built-in ``facts`` niche
* one video for the custom / "Other" niche exercised with ``cricket``

For every MP4 found, this module runs ``ffprobe`` (JSON output) and
asserts:

* valid MP4-family container (``format_name`` contains ``mp4``/``mov``),
* at least one video stream and at least one audio stream,
* duration plausible for a ~1-minute video (30–120 s),
* non-trivial file size (> 100 KiB).

Identification (facts vs cricket) is derived from filenames first and
from any manifests / logs / JSON sidecars the producing team wrote into
the same directory (see ``_load_sidecar_texts`` / ``_classify``).

Failure semantics (deliberate)
------------------------------
The ``outputs/`` tree is gitignored, so a clean checkout (CI) never
contains it.  When ``outputs/round2/`` is *absent entirely*, these
tests **SKIP** with a clear reason — the same pattern
``tests/unit/test_smoke.py`` uses for the sibling roadmap tree that
only exists in the dev workspace.  But whenever the directory *exists*
(a dev machine, where the E2E team has run), the fail-loud design is
unchanged: an existing-but-empty directory, a missing niche video or
an invalid MP4 **FAIL** with a clear message — acceptance of Round 2
requires real files on disk, and a missing output there is a blocker,
not a pass.  The other skip in this module is the standard ``ffmpeg``
marker behaviour from ``tests/conftest.py`` when no ffprobe binary
exists at all (not the case on the reference VM: FFmpeg 8.x).

Run from the repo root::

    backend/.venv/bin/python -m pytest tests/integration/test_e2e_outputs.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.ffmpeg]

# ---------------------------------------------------------------------------
# Locations & thresholds
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "outputs" / "round2"

EXPECTED_NICHES = ("facts", "cricket")

MIN_DURATION_S = 30.0
MAX_DURATION_S = 120.0
MIN_FILE_SIZE_BYTES = 100 * 1024  # 100 KiB — a real 1-min A/V render is far larger

SIDECAR_SUFFIXES = {".json", ".md", ".log", ".txt", ".yaml", ".yml"}


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


#: Directory names that hold intermediates / scratch, never deliverables:
#: ``.scratch`` = other workers' pytest basetemp; ``_work`` = the render
#: pipeline's per-segment work dir under ``pack/`` (scene .ts segments).
_NON_DELIVERABLE_DIR_NAMES = {"_work"}


def _is_deliverable(path: Path) -> bool:
    """True for real outputs; False for scratch / intermediate artefacts.

    Other Round 2 workers run pytest with a basetemp under
    ``outputs/round2/.scratch/`` — those fixture MP4s (including
    intentionally corrupt ones) are not E2E deliverables, so anything
    under a dot-directory is excluded from discovery.  The same applies
    to the renderer's ``_work`` segment directory inside a project pack.
    """
    try:
        rel_parts = path.relative_to(OUTPUT_DIR).parts
    except ValueError:
        return False
    return not any(
        part.startswith(".") or part in _NON_DELIVERABLE_DIR_NAMES
        for part in rel_parts
    )


def _discover_mp4s() -> list[Path]:
    """All deliverable MP4s under outputs/round2/ (recursive, sorted)."""
    if not OUTPUT_DIR.is_dir():
        return []
    return sorted(
        p for p in OUTPUT_DIR.rglob("*.mp4") if p.is_file() and _is_deliverable(p)
    )


def _load_sidecar_texts() -> dict[Path, str]:
    """Map of sidecar (manifest/log/json/txt) path -> text content.

    Whatever the E2E team wrote alongside the videos — JSON manifests,
    run logs, READMEs — is used for provenance and niche identification.
    Unreadable / binary files are ignored rather than failing discovery.
    """
    texts: dict[Path, str] = {}
    if not OUTPUT_DIR.is_dir():
        return texts
    for path in sorted(OUTPUT_DIR.rglob("*")):
        if (
            path.is_file()
            and _is_deliverable(path)
            and path.suffix.lower() in SIDECAR_SUFFIXES
        ):
            try:
                texts[path] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return texts


def _classify(mp4: Path, sidecars: dict[Path, str]) -> str | None:
    """Return 'facts', 'cricket', or None for *mp4*.

    Order of evidence:
    1. the filename / parent-directory name itself,
    2. a same-stem sidecar (``<stem>.json`` etc.) mentioning the niche,
    3. any sidecar text that mentions both the file name and a niche.
    """
    hay_name = f"{mp4.parent.name}/{mp4.name}".lower()
    for niche in EXPECTED_NICHES:
        if niche in hay_name:
            return niche

    stem = mp4.stem.lower()
    for path, text in sidecars.items():
        if path.stem.lower() == stem:
            low = text.lower()
            for niche in EXPECTED_NICHES:
                if niche in low:
                    return niche

    for _path, text in sidecars.items():
        low = text.lower()
        if mp4.name.lower() in low:
            for niche in EXPECTED_NICHES:
                if niche in low:
                    return niche
    return None


def _classified_videos() -> dict[str, Path]:
    """Map niche -> MP4 path for every classifiable video."""
    sidecars = _load_sidecar_texts()
    out: dict[str, Path] = {}
    for mp4 in _discover_mp4s():
        niche = _classify(mp4, sidecars)
        if niche is not None and niche not in out:
            out[niche] = mp4
    return out


def _require_outputs() -> list[Path]:
    """Return the MP4 list; SKIP if the outputs tree is absent, else FAIL loud.

    Absent directory = clean checkout (``outputs/`` is gitignored) → skip,
    following the sibling-tree pattern in ``tests/unit/test_smoke.py``.
    Existing directory = the E2E team has run here → keep failing loud.
    """
    if not OUTPUT_DIR.is_dir():
        pytest.skip(
            f"Round 2 E2E outputs directory not in this checkout: {OUTPUT_DIR} "
            "(outputs/ is gitignored; these tests run on dev machines where "
            "the E2E integration team has produced the videos)."
        )
    mp4s = _discover_mp4s()
    if not mp4s:
        present = sorted(str(p.relative_to(OUTPUT_DIR)) for p in OUTPUT_DIR.rglob("*"))
        pytest.fail(
            f"No MP4 files found under {OUTPUT_DIR}. "
            f"Directory contents: {present or '(empty)'} — "
            "expected a 'facts' MP4 and a 'cricket' MP4 for acceptance test E."
        )
    return mp4s


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------


def _ffprobe(path: Path) -> dict:
    """Run ffprobe with JSON output and return the parsed result."""
    ffprobe = shutil.which("ffprobe")
    assert ffprobe, "ffprobe binary not found on PATH"
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"ffprobe failed on {path.name} (exit {proc.returncode}): {proc.stderr.strip()}"
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"ffprobe did not return valid JSON for {path.name}: {exc}")


def _probe_facts(path: Path) -> dict:
    """Normalised facts about one video, used by several tests."""
    data = _ffprobe(path)
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    duration_raw = fmt.get("duration")
    if duration_raw is None and streams:
        duration_raw = streams[0].get("duration")
    return {
        "path": path,
        "format_name": fmt.get("format_name", ""),
        "duration_s": float(duration_raw) if duration_raw is not None else None,
        "size_bytes": path.stat().st_size,
        "video_streams": [s for s in streams if s.get("codec_type") == "video"],
        "audio_streams": [s for s in streams if s.get("codec_type") == "audio"],
        "raw": data,
    }


# ---------------------------------------------------------------------------
# Tests — presence & identification
# ---------------------------------------------------------------------------


def test_round2_output_dir_exists_and_has_mp4s():
    """outputs/round2/ must exist and contain at least two MP4s."""
    mp4s = _require_outputs()
    assert len(mp4s) >= 2, (
        f"Expected at least 2 MP4s in {OUTPUT_DIR} (facts + cricket), "
        f"found {len(mp4s)}: {[p.name for p in mp4s]}"
    )


@pytest.mark.parametrize("niche", EXPECTED_NICHES)
def test_expected_niche_video_present(niche: str):
    """One identifiable MP4 per expected niche (facts, cricket)."""
    _require_outputs()
    classified = _classified_videos()
    all_names = [p.name for p in _discover_mp4s()]
    assert niche in classified, (
        f"No MP4 identifiable as the '{niche}' video in {OUTPUT_DIR}. "
        f"Files present: {all_names}; classified: "
        f"{ {k: v.name for k, v in classified.items()} }. "
        "Identification uses filenames and any manifests/logs in the directory."
    )


def test_sidecar_manifest_or_log_present():
    """At least one manifest/log/JSON sidecar should document provenance."""
    _require_outputs()
    sidecars = _load_sidecar_texts()
    assert sidecars, (
        f"No manifest, log or JSON sidecar found under {OUTPUT_DIR} — "
        "the producing team left no provenance / timing record alongside "
        "the videos (acceptance test E expects e.g. a JSON manifest)."
    )


# ---------------------------------------------------------------------------
# Tests — per-video ffprobe validation
# ---------------------------------------------------------------------------


def test_each_video_is_valid_mp4_with_av_streams():
    """Every MP4: MP4-family container, >=1 video stream, >=1 audio stream."""
    for mp4 in _require_outputs():
        facts = _probe_facts(mp4)
        fmt = facts["format_name"].lower()
        assert "mp4" in fmt or "mov" in fmt, (
            f"{mp4.name}: not an MP4-family container (format_name={fmt!r})"
        )
        assert facts["video_streams"], f"{mp4.name}: no video stream found"
        assert facts["audio_streams"], f"{mp4.name}: no audio stream found"


def test_each_video_duration_plausible_for_one_minute():
    """Every MP4 duration must be within 30–120 s (target ~60 s)."""
    for mp4 in _require_outputs():
        facts = _probe_facts(mp4)
        dur = facts["duration_s"]
        assert dur is not None, f"{mp4.name}: ffprobe reported no duration"
        assert MIN_DURATION_S <= dur <= MAX_DURATION_S, (
            f"{mp4.name}: duration {dur:.2f}s outside plausible range "
            f"[{MIN_DURATION_S:.0f}s, {MAX_DURATION_S:.0f}s] for a ~1-minute video"
        )


def test_each_video_file_size_nontrivial():
    """Every MP4 must be larger than 100 KiB (not a stub / empty render)."""
    for mp4 in _require_outputs():
        size = mp4.stat().st_size
        assert size > MIN_FILE_SIZE_BYTES, (
            f"{mp4.name}: file size {size} bytes is trivially small "
            f"(<= {MIN_FILE_SIZE_BYTES} bytes) for a ~1-minute video"
        )


def test_each_video_streams_have_codecs():
    """Video/audio streams must carry real codec names (sanity check)."""
    for mp4 in _require_outputs():
        facts = _probe_facts(mp4)
        for stream in facts["video_streams"] + facts["audio_streams"]:
            assert stream.get("codec_name"), (
                f"{mp4.name}: a {stream.get('codec_type')} stream has no codec_name"
            )
