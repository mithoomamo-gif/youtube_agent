"""Text-hash chunk cache.

Cache key = hash(text + voice_id + rate + pitch + dict_version + provider +
model). A pronunciation fix bumps ``dict_version`` and therefore re-renders
only chunks whose text contains the fixed word (their compiled text changes)
— every other chunk keeps its key and its audio.

Two backends: in-memory dict, and a file-backed store (JSON index + one
audio file per entry) that survives app restarts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass

from .base import TTSResult


def compute_cache_key(text: str, voice_id: str, rate_percent: float,
                      pitch_hz: float, dict_version: str, provider: str,
                      model: str) -> str:
    payload = json.dumps({
        "text": text, "voice_id": voice_id,
        "rate": round(float(rate_percent), 3), "pitch": round(float(pitch_hz), 3),
        "dict_version": dict_version, "provider": provider, "model": model,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CacheEntry:
    result: TTSResult


class DictCache:
    """In-memory cache (tests, short-lived jobs)."""

    def __init__(self):
        self._store: dict[str, TTSResult] = {}

    def get(self, key: str) -> TTSResult | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        return _clone(hit)

    def put(self, key: str, result: TTSResult) -> None:
        self._store[key] = _clone(result)


class FileCache:
    """File-backed cache: ``<dir>/index.json`` + ``<dir>/<key>.audio``."""

    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self._index_path = os.path.join(directory, "index.json")
        self._index: dict[str, dict] = {}
        if os.path.exists(self._index_path):
            try:
                with open(self._index_path, encoding="utf-8") as fh:
                    self._index = json.load(fh)
            except (json.JSONDecodeError, OSError):
                self._index = {}

    def _audio_path(self, key: str) -> str:
        return os.path.join(self.directory, f"{key}.audio")

    def get(self, key: str) -> TTSResult | None:
        meta = self._index.get(key)
        if not meta:
            return None
        audio = b""
        path = self._audio_path(key)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                audio = fh.read()
        elif meta.get("audio_b64"):
            audio = base64.b64decode(meta["audio_b64"])
        return TTSResult(
            provider=meta["provider"], model=meta["model"],
            voice_id=meta["voice_id"], billed_chars=meta["billed_chars"],
            duration_s=meta["duration_s"], audio_bytes=audio,
            audio_path=path if os.path.exists(path) else meta.get("audio_path"),
            cost_usd=meta.get("cost_usd", 0.0),
            word_timestamps=meta.get("word_timestamps", []),
            cache_key=key, from_cache=True)

    def put(self, key: str, result: TTSResult) -> None:
        if result.audio_bytes:
            with open(self._audio_path(key), "wb") as fh:
                fh.write(result.audio_bytes)
        self._index[key] = {
            "provider": result.provider, "model": result.model,
            "voice_id": result.voice_id, "billed_chars": result.billed_chars,
            "duration_s": result.duration_s, "cost_usd": result.cost_usd,
            "audio_path": result.audio_path,
            "word_timestamps": result.word_timestamps,
        }
        with open(self._index_path, "w", encoding="utf-8") as fh:
            json.dump(self._index, fh, ensure_ascii=False)


def _clone(result: TTSResult) -> TTSResult:
    return TTSResult(
        provider=result.provider, model=result.model, voice_id=result.voice_id,
        billed_chars=result.billed_chars, duration_s=result.duration_s,
        audio_bytes=result.audio_bytes, audio_path=result.audio_path,
        cost_usd=result.cost_usd, duration_estimated=result.duration_estimated,
        word_timestamps=list(result.word_timestamps),
        cache_key=result.cache_key, from_cache=True)
