"""Deriving a project's section map (chapter boundaries in seconds).

What the produce pipeline actually stores (studied 2026-10-23 against
``outputs/round3/phase0/`` and ``outputs/round4/``)
-------------------------------------------------------------------
* ``bundle.json`` — the content bundle: ``chapters`` are
  ``{index (1-based), title, text}``.  **No timings.**  Word counts
  are recoverable from ``text``.
* ``visuals.json`` / ``visuals/scene_manifest.json`` — the scene
  manifest: every scene carries ``chapter_index`` (0-based),
  ``start_s`` / ``end_s`` / ``duration_s``, plus
  ``total_duration_s`` at the top level.  In the pipeline
  (``app.integration.visuals_stage``) scenes are laid end-to-end from
  *measured* per-chapter voiceover durations, word-weighted inside
  each chapter — so aggregating scenes per ``chapter_index`` yields
  measured chapter boundaries.
* ``audio/chapter_NN_*.wav`` + sidecar JSONs — one WAV per chapter;
  the sidecars carry word/char counts, NOT durations (durations are
  recoverable from the WAV headers).  The narration assembly inserts
  a pause between chapters (preset default 650 ms).
* ``captions.srt`` — Whisper-derived cues with real timings but
  transcribed (not script) text, and a cue word count that can
  diverge materially from the script's (Round 3 facts pack: ~366 SRT
  words vs 541 script words).  Usable for *word-count alignment*
  only, never for exact boundaries.

Source precedence (best precision first)
-----------------------------------------
1. ``scene_manifest`` — measured chapter audio, scenes laid end to
   end: boundaries exact to the manifest's millisecond rounding.
   Precision: "measured".
2. ``voiceover_chapters`` — per-chapter WAV durations summed in
   order.  Precision: measured per chapter, but inter-chapter pauses
   in the assembled narration are not in the WAVs, so later
   boundaries can drift early by up to (n-1) x the chapter pause.
3. ``srt_wordcount`` — each chapter boundary is placed where the
   SRT's cumulative cue-word count crosses the chapter's cumulative
   script-word fraction.  Precision: estimated, +/- one cue
   (~ up to 7 s), worse when Whisper word counts diverge.
4. ``word_proportional`` — boundaries proportional to chapter word
   counts over the known total duration.  Precision: estimated;
   assumes a constant speaking rate across chapters.

:func:`load_section_map` walks a project directory (the layout above)
and returns the best map available, or ``None`` — the caller then
reports the honest ``unmapped`` state.
"""

from __future__ import annotations

import json
import re
import wave
from pathlib import Path
from typing import Any, Sequence

from .mapping import SectionMap, SectionSpan

__all__ = [
    "PRECISION_MEASURED_MANIFEST",
    "PRECISION_SRT_WORDCOUNT",
    "PRECISION_VOICEOVER",
    "PRECISION_WORD_PROPORTIONAL",
    "SOURCE_SCENE_MANIFEST",
    "SOURCE_SRT_WORDCOUNT",
    "SOURCE_VOICEOVER",
    "SOURCE_WORD_PROPORTIONAL",
    "load_section_map",
    "section_map_from_chapter_durations",
    "section_map_from_scene_manifest",
    "section_map_from_srt_wordcount",
    "section_map_from_word_counts",
    "titles_and_word_counts",
]

SOURCE_SCENE_MANIFEST = "scene_manifest"
SOURCE_VOICEOVER = "voiceover_chapters"
SOURCE_SRT_WORDCOUNT = "srt_wordcount"
SOURCE_WORD_PROPORTIONAL = "word_proportional"

PRECISION_MEASURED_MANIFEST = (
    "measured: boundaries aggregated from the scene manifest, whose "
    "scenes are laid end-to-end from measured per-chapter voiceover "
    "durations (millisecond rounding)"
)
PRECISION_VOICEOVER = (
    "measured per-chapter audio (WAV headers); inter-chapter pauses "
    "in the assembled narration are excluded, so later boundaries "
    "may sit early by up to (chapters-1) x the chapter pause"
)
PRECISION_SRT_WORDCOUNT = (
    "estimated: script chapters aligned to the SRT by cumulative "
    "word count; +/- one cue (~7s), worse when the transcription's "
    "word count diverges from the script's"
)
PRECISION_WORD_PROPORTIONAL = (
    "estimated: boundaries proportional to chapter word counts; "
    "assumes a constant speaking rate across chapters"
)


def titles_and_word_counts(chapters: Sequence[Any]) -> tuple[list[str], list[int]]:
    """(titles, word counts) from bundle-shaped chapter records.

    Accepts dicts or objects with ``title`` / ``text`` (the
    ``ContentBundle`` chapter shape); falls back to ``Chapter N``
    titles so a map is never blocked by a missing title.
    """
    titles: list[str] = []
    counts: list[int] = []
    for i, chapter in enumerate(chapters):
        if isinstance(chapter, dict):
            title = chapter.get("title") or chapter.get("name")
            text = chapter.get("text") or chapter.get("narration") or ""
        else:
            title = getattr(chapter, "title", None) or getattr(chapter, "name", None)
            text = getattr(chapter, "text", "") or ""
        titles.append(str(title).strip() if title else f"Chapter {i + 1}")
        counts.append(len(str(text).split()))
    return titles, counts


def _spans(titles: Sequence[str], boundaries: Sequence[float]) -> list[SectionSpan]:
    return [
        SectionSpan(
            index=i,
            title=titles[i] if i < len(titles) else f"Chapter {i + 1}",
            start_s=round(float(boundaries[i]), 3),
            end_s=round(float(boundaries[i + 1]), 3),
        )
        for i in range(len(boundaries) - 1)
    ]


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------


def section_map_from_scene_manifest(
    manifest: Any, titles: Sequence[str] | None = None
) -> SectionMap | None:
    """Aggregate a scene manifest's scenes into chapter boundaries.

    ``manifest`` is the parsed ``visuals.json`` (scenes under
    ``"scene_manifest"``) or ``scene_manifest.json`` (scenes under
    ``"scenes"``); each scene needs ``chapter_index``, ``start_s``
    and ``end_s``.  Returns None when no usable scenes are present.
    """
    if isinstance(manifest, dict):
        scenes = manifest.get("scenes") or manifest.get("scene_manifest") or []
        total = manifest.get("total_duration_s")
    elif isinstance(manifest, (list, tuple)):
        scenes, total = list(manifest), None
    else:
        return None
    per_chapter: dict[int, tuple[float, float]] = {}
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        try:
            ci = int(scene["chapter_index"])
            start, end = float(scene["start_s"]), float(scene["end_s"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        lo, hi = per_chapter.get(ci, (start, end))
        per_chapter[ci] = (min(lo, start), max(hi, end))
    if not per_chapter:
        return None
    ordered = [per_chapter[ci] for ci in sorted(per_chapter)]
    boundaries = [ordered[0][0]] + [hi for _, hi in ordered]
    # Scenes are laid end-to-end, so interior boundaries coincide;
    # enforce monotonicity defensively against hand-built manifests.
    for i in range(1, len(boundaries)):
        boundaries[i] = max(boundaries[i], boundaries[i - 1])
    names = list(titles) if titles else [f"Chapter {i + 1}" for i in range(len(ordered))]
    duration = float(total) if total else boundaries[-1]
    return SectionMap(
        sections=_spans(names, boundaries),
        source=SOURCE_SCENE_MANIFEST,
        precision=PRECISION_MEASURED_MANIFEST,
        duration_s=duration,
    )


def section_map_from_chapter_durations(
    titles: Sequence[str],
    durations_s: Sequence[float],
    *,
    source: str = SOURCE_VOICEOVER,
    precision: str = PRECISION_VOICEOVER,
) -> SectionMap | None:
    """Boundaries from measured per-chapter durations, laid in order."""
    durations = [float(d) for d in durations_s]
    if not durations or any(d <= 0 for d in durations):
        return None
    boundaries = [0.0]
    for d in durations:
        boundaries.append(boundaries[-1] + d)
    return SectionMap(
        sections=_spans(list(titles), boundaries),
        source=source,
        precision=precision,
        duration_s=boundaries[-1],
    )


def section_map_from_word_counts(
    titles: Sequence[str], word_counts: Sequence[int], duration_s: float
) -> SectionMap | None:
    """Word-proportional boundaries over a known total duration."""
    total_words = sum(word_counts)
    if total_words <= 0 or duration_s <= 0 or not word_counts:
        return None
    boundaries = [0.0]
    cumulative = 0
    for count in word_counts[:-1]:
        cumulative += int(count)
        boundaries.append(duration_s * cumulative / total_words)
    boundaries.append(float(duration_s))
    return SectionMap(
        sections=_spans(list(titles), boundaries),
        source=SOURCE_WORD_PROPORTIONAL,
        precision=PRECISION_WORD_PROPORTIONAL,
        duration_s=float(duration_s),
    )


def section_map_from_srt_wordcount(
    chapters: Sequence[Any], srt_text: str, duration_s: float | None = None
) -> SectionMap | None:
    """Align chapter boundaries to an SRT by cumulative word count.

    The boundary after chapter *k* is placed at the moment the SRT's
    cumulative cue-word count crosses chapter *k*'s cumulative share
    of the script's words (linearly interpolated inside the crossing
    cue).  Returns None when either side has no words.
    """
    from app.pipeline.subtitles.srt_builder import parse_srt

    titles, counts = titles_and_word_counts(chapters)
    total_script_words = sum(counts)
    if total_script_words <= 0 or len(counts) < 1:
        return None
    cues = parse_srt(srt_text or "")
    cue_words = [len(cue.text.split()) for cue in cues]
    total_cue_words = sum(cue_words)
    if total_cue_words <= 0:
        return None
    total_duration = (
        float(duration_s) if duration_s else (cues[-1].end if cues else 0.0)
    )
    if total_duration <= 0:
        return None

    def crossing_time(target_words: float) -> float:
        seen = 0
        for cue, n_words in zip(cues, cue_words):
            if n_words and seen + n_words >= target_words:
                frac = (target_words - seen) / n_words
                return cue.start + frac * max(0.0, cue.end - cue.start)
            seen += n_words
        return total_duration

    boundaries = [0.0]
    cumulative = 0
    for count in counts[:-1]:
        cumulative += int(count)
        target = total_cue_words * cumulative / total_script_words
        boundaries.append(min(crossing_time(target), total_duration))
    boundaries.append(total_duration)
    for i in range(1, len(boundaries)):
        boundaries[i] = max(boundaries[i], boundaries[i - 1] + 0.001)
    boundaries[-1] = total_duration
    return SectionMap(
        sections=_spans(titles, boundaries),
        source=SOURCE_SRT_WORDCOUNT,
        precision=PRECISION_SRT_WORDCOUNT,
        duration_s=total_duration,
    )


def _wav_duration_s(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else None
    except Exception:
        return None


_CHAPTER_WAV_RE = re.compile(r"chapter_(\d+)_", re.IGNORECASE)


def section_map_from_voiceover_dir(
    audio_dir: Path, titles: Sequence[str]
) -> SectionMap | None:
    """Boundaries from the per-chapter WAV files in ``audio_dir``."""
    wavs: list[tuple[int, Path]] = []
    for path in Path(audio_dir).glob("chapter_*.wav"):
        match = _CHAPTER_WAV_RE.match(path.name)
        if match:
            wavs.append((int(match.group(1)), path))
    if not wavs:
        return None
    wavs.sort()
    durations = [_wav_duration_s(path) for _, path in wavs]
    if any(d is None for d in durations):
        return None
    return section_map_from_chapter_durations(
        titles, [d for d in durations if d is not None]
    )


# ---------------------------------------------------------------------------
# Pack / project-directory loader
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _first_existing(base: Path, relatives: Sequence[str]) -> Path | None:
    for rel in relatives:
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return None


def load_section_map(project_dir: Path | str) -> SectionMap | None:
    """Best available section map for a produced project's directory.

    Searches the layouts the pipeline actually writes (Round 3/4
    packs): ``bundle.json`` for titles, scene manifests at
    ``visuals.json`` / ``scene_manifest.json`` /
    ``visuals/scene_manifest.json``, ``audio/chapter_*.wav``,
    ``captions.srt`` (project dir or its ``pack/``), and durations
    from the manifest / ``voice.json`` / ``publish_manifest.json``.
    Returns None when the directory holds nothing derivable — the
    caller's honest ``unmapped`` state, never a fabricated map.
    """
    base = Path(project_dir)
    if not base.is_dir():
        return None

    bundle = None
    bundle_path = _first_existing(base, ["bundle.json", "content/bundle.json"])
    if bundle_path is not None:
        bundle = _read_json(bundle_path)
    chapters = (bundle or {}).get("chapters") or []
    titles, word_counts = titles_and_word_counts(chapters)

    duration: float | None = None
    voice_path = _first_existing(base, ["voice.json"])
    if voice_path is not None:
        voice = _read_json(voice_path) or {}
        if voice.get("duration_s"):
            duration = float(voice["duration_s"])
    if duration is None:
        manifest_path = _first_existing(
            base, ["publish_manifest.json", "pack/publish_manifest.json"]
        )
        if manifest_path is not None:
            manifest = _read_json(manifest_path) or {}
            if manifest.get("duration_seconds"):
                duration = float(manifest["duration_seconds"])

    # 1) Scene manifest (measured).
    for rel in ("visuals.json", "scene_manifest.json", "visuals/scene_manifest.json"):
        path = _first_existing(base, [rel])
        if path is None:
            continue
        parsed = _read_json(path)
        if parsed is None:
            continue
        try:
            result = section_map_from_scene_manifest(parsed, titles or None)
        except ValueError:
            result = None
        if result is not None:
            return result

    # 2) Per-chapter voiceover WAVs (measured, pause caveat).
    audio_dir = base / "audio"
    if titles and audio_dir.is_dir():
        try:
            result = section_map_from_voiceover_dir(audio_dir, titles)
        except ValueError:
            result = None
        if result is not None:
            return result

    # 3) SRT word-count alignment (estimated).
    srt_path = _first_existing(base, ["captions.srt", "pack/captions.srt"])
    if srt_path is not None and chapters:
        try:
            srt_text = srt_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            srt_text = ""
        try:
            result = section_map_from_srt_wordcount(chapters, srt_text, duration)
        except ValueError:
            result = None
        if result is not None:
            return result

    # 4) Word-proportional over the known duration (estimated).
    if chapters and duration:
        return section_map_from_word_counts(titles, word_counts, duration)
    return None
