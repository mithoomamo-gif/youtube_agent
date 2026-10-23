"""Stitching plan + ``voice_manifest.json``.

Actual FFmpeg calls are another team's job. This module produces the
ordered chunk list and the gap rules they consume:

* master format: WAV 44.1 kHz mono
* 250-350 ms between chunks inside a chapter (preset-driven)
* chapter pauses 600-900 ms by default, preset-driven (500-1,000 ms across
  the niche presets)
* final loudness target -14 LUFS integrated / -1 dBTP (applied downstream)
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

TARGET_SAMPLE_RATE = 44100
TARGET_CHANNELS = 1
DEFAULT_CHUNK_GAP_MS = 300
DEFAULT_CHAPTER_PAUSE_MS = 750


@dataclass
class ManifestChunk:
    chunk_no: int
    chapter_no: int
    provider: str
    voice: str
    preset: str | None
    chars: int
    billed_cost: float
    duration_s: float
    cache_key: str
    status: str                      # ok | cache_hit | failed
    audio_path: str | None = None
    gap_after_ms: int = DEFAULT_CHUNK_GAP_MS


@dataclass
class StitchItem:
    chunk_no: int
    chapter_no: int
    audio_path: str | None
    duration_s: float
    gap_after_ms: int                # silence to insert AFTER this chunk


@dataclass
class StitchPlan:
    format: str = "wav"
    sample_rate: int = TARGET_SAMPLE_RATE
    channels: int = TARGET_CHANNELS
    loudness_target_lufs: float = -14.0
    true_peak_dbtp: float = -1.0
    items: list[StitchItem] = field(default_factory=list)

    @property
    def total_duration_s(self) -> float:
        return sum(i.duration_s + i.gap_after_ms / 1000.0 for i in self.items)


@dataclass
class VoiceManifest:
    video_id: str
    dict_version: str = ""
    language: str = "en"
    preset: str | None = None
    total_cost_usd: float = 0.0
    providers_by_chapter: dict[int, str] = field(default_factory=dict)
    flagged_tokens: list[str] = field(default_factory=list)
    chunks: list[ManifestChunk] = field(default_factory=list)
    stitch_plan: StitchPlan = field(default_factory=StitchPlan)

    def to_dict(self) -> dict:
        return asdict(self)

    def write(self, directory: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "voice_manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        return path


def build_stitch_plan(manifest_chunks: list[ManifestChunk],
                      chapter_pause_ms: int) -> StitchPlan:
    """Order chunks, applying chunk gaps inside a chapter and the chapter
    pause after each chapter's last chunk (no trailing gap at the end)."""
    ordered = sorted(manifest_chunks, key=lambda c: c.chunk_no)
    items: list[StitchItem] = []
    for idx, chunk in enumerate(ordered):
        is_last = idx == len(ordered) - 1
        next_chunk = ordered[idx + 1] if not is_last else None
        if is_last:
            gap = 0
        elif next_chunk and next_chunk.chapter_no != chunk.chapter_no:
            gap = chapter_pause_ms
        else:
            gap = chunk.gap_after_ms
        chunk.gap_after_ms = gap
        items.append(StitchItem(chunk_no=chunk.chunk_no,
                                chapter_no=chunk.chapter_no,
                                audio_path=chunk.audio_path,
                                duration_s=chunk.duration_s,
                                gap_after_ms=gap))
    return StitchPlan(items=items)
