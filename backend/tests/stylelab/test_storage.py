"""Storage seam tests (owned file-based seam — no intake table exists)."""
from __future__ import annotations

import json

from app.style_analysis.storage import StyleAnalysisStore, slugify_key

from .test_schema import full_analysis


def test_slugify():
    assert slugify_key("Comp Vid/1!") == "comp-vid-1"
    assert slugify_key("  ") == "unknown"


def test_save_load_round_trip(tmp_path):
    store = StyleAnalysisStore(tmp_path)
    a = full_analysis()
    stored = store.save(a)
    assert stored.analysis_path.exists()
    assert stored.breakdown_path.exists()
    assert stored.index_path.exists()
    assert "# Style Lab" in stored.breakdown_path.read_text(encoding="utf-8")
    loaded = store.load("comp-vid-1")
    assert loaded == a
    # index entry reflects the save
    entries = store.list_entries()
    assert len(entries) == 1
    assert entries[0].competitor_video_id == "comp-vid-1"
    assert entries[0].channel_id == "chan-1"
    assert entries[0].dominant_visual_label == "uncertain"
    index = json.loads(stored.index_path.read_text(encoding="utf-8"))
    assert index["entries"][0]["analysis_id"] == a.analysis_id


def test_save_twice_latest_wins_one_entry(tmp_path):
    store = StyleAnalysisStore(tmp_path)
    a = full_analysis()
    store.save(a)
    a.overall_confidence = 0.99
    store.save(a)
    assert len(store.list_entries()) == 1
    assert store.load("comp-vid-1").overall_confidence == 0.99


def test_load_missing_returns_none(tmp_path):
    store = StyleAnalysisStore(tmp_path)
    assert store.load("nope") is None
    assert store.load_breakdown("nope") is None
