"""Content-hash cache for generated images.

Identical (effective prompt + negative + style profile/version + seed +
model + aspect) requests return the cached image without regenerating —
re-renders and repeated topics across videos reuse assets (roadmap §21).
"""

from __future__ import annotations

import hashlib
import json

from .models import ImageResult


def content_hash(
    *,
    prompt: str,
    negative_prompt: str,
    style_profile_id: str | None,
    style_version: int | None,
    seed: int | None,
    model_id: str,
    aspect: str,
) -> str:
    payload = json.dumps(
        {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style_profile_id": style_profile_id,
            "style_version": style_version,
            "seed": seed,
            "model_id": model_id,
            "aspect": aspect,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ImageCache:
    """Process-local image cache keyed by :func:`content_hash`."""

    def __init__(self) -> None:
        self._store: dict[str, ImageResult] = {}

    def get(self, key: str) -> ImageResult | None:
        return self._store.get(key)

    def put(self, key: str, result: ImageResult) -> None:
        self._store[key] = result

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()
