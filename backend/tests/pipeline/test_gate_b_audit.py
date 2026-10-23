"""Tests for the Gate B conditional-policy audit log.

Covers the fixed contract in ``app.pipeline.gates.audit``:

* ``record_gate_b_event`` appends exactly one JSON object per line,
  creates parent directories, never overwrites prior lines, enriches
  events with ``ts`` / ``policy_version`` / ``event_id`` and rejects
  events missing a required key.
* ``read_gate_b_events`` parses the log back in order and returns
  ``[]`` for a missing file.
* ``asset_ids_from_ledger`` extracts the real stock IDs from rows
  shaped exactly like the real Round 3 cricket licence ledger and
  skips the ID-less / generated rows (voice / music).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.pipeline.gates.audit import (  # noqa: E402
    EVENT_APPROVAL_RECORDED,
    EVENT_BLOCKED,
    EVENT_CONDITIONAL_MINTED,
    EVENT_PUBLISH_RELEASED,
    asset_ids_from_ledger,
    read_gate_b_events,
    record_gate_b_event,
)


def _event(**overrides):
    event = {
        "event_type": EVENT_CONDITIONAL_MINTED,
        "manifest_hash": "abc123manifest",
        "mode": "review_first",
        "outcome": "conditional",
        "qc_flags": ["QC_WARN"],
        "minor_flags": ["BLACK_SEGMENT_BRIEF"],
        "asset_ids": ["189845"],
        "actor": "system",
        "notes": "0.6s black passage",
    }
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# Event-type constants
# ---------------------------------------------------------------------------


def test_event_type_constants():
    assert EVENT_CONDITIONAL_MINTED == "conditional_token_minted"
    assert EVENT_APPROVAL_RECORDED == "approval_recorded"
    assert EVENT_PUBLISH_RELEASED == "publish_released"
    assert EVENT_BLOCKED == "blocked"


# ---------------------------------------------------------------------------
# record + read: creation, appending, enrichment
# ---------------------------------------------------------------------------


def test_append_creates_file_and_parent_dirs(tmp_path):
    sink = tmp_path / "nested" / "deeper" / "gate_b_audit.jsonl"
    assert not sink.parent.exists()

    record_gate_b_event(sink, _event())

    assert sink.is_file()
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event_type"] == EVENT_CONDITIONAL_MINTED
    assert parsed["manifest_hash"] == "abc123manifest"


def test_two_events_append_no_overwrite_and_read_back_in_order(tmp_path):
    sink = tmp_path / "gate_b_audit.jsonl"

    record_gate_b_event(sink, _event())
    record_gate_b_event(
        sink,
        _event(
            event_type=EVENT_APPROVAL_RECORDED,
            mode="review_first",
            outcome="approved",
            actor="human-reviewer",
        ),
    )

    # Exactly two lines -- the second write appended, not overwrote.
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    events = read_gate_b_events(sink)
    assert len(events) == 2
    assert events[0]["event_type"] == EVENT_CONDITIONAL_MINTED
    assert events[1]["event_type"] == EVENT_APPROVAL_RECORDED
    assert events[1]["outcome"] == "approved"

    # Enrichment present on every event.
    for event in events:
        assert event["ts"]  # UTC ISO-8601 timestamp string
        assert "T" in event["ts"]
        assert event["event_id"]  # uuid4 hex
        assert len(event["event_id"]) == 32
        int(event["event_id"], 16)  # parses as hex
        assert event["policy_version"]

    # Each event gets its own id.
    assert events[0]["event_id"] != events[1]["event_id"]

    # Optional keys passed through untouched.
    assert events[0]["qc_flags"] == ["QC_WARN"]
    assert events[0]["minor_flags"] == ["BLACK_SEGMENT_BRIEF"]
    assert events[0]["asset_ids"] == ["189845"]
    assert events[0]["actor"] == "system"
    assert events[0]["notes"] == "0.6s black passage"


def test_record_accepts_str_path(tmp_path):
    sink = tmp_path / "gate_b_audit.jsonl"
    record_gate_b_event(str(sink), _event())
    assert len(read_gate_b_events(str(sink))) == 1


def test_caller_supplied_policy_version_is_preserved(tmp_path):
    sink = tmp_path / "gate_b_audit.jsonl"
    record_gate_b_event(sink, _event(policy_version="9.9-test"))
    (event,) = read_gate_b_events(sink)
    assert event["policy_version"] == "9.9-test"


def test_caller_dict_is_not_mutated(tmp_path):
    sink = tmp_path / "gate_b_audit.jsonl"
    original = _event()
    snapshot = dict(original)
    record_gate_b_event(sink, original)
    assert original == snapshot
    assert "ts" not in original
    assert "event_id" not in original


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_key", ["event_type", "manifest_hash", "mode", "outcome"]
)
def test_missing_required_key_raises_value_error(tmp_path, missing_key):
    sink = tmp_path / "gate_b_audit.jsonl"
    event = _event()
    del event[missing_key]

    with pytest.raises(ValueError) as excinfo:
        record_gate_b_event(sink, event)

    assert missing_key in str(excinfo.value)
    # Nothing was written for the rejected event.
    assert not sink.exists()


def test_missing_multiple_keys_are_all_listed(tmp_path):
    sink = tmp_path / "gate_b_audit.jsonl"
    with pytest.raises(ValueError) as excinfo:
        record_gate_b_event(sink, {"event_type": EVENT_BLOCKED})
    message = str(excinfo.value)
    assert "manifest_hash" in message
    assert "mode" in message
    assert "outcome" in message


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_read_missing_file_returns_empty_list(tmp_path):
    assert read_gate_b_events(tmp_path / "does_not_exist.jsonl") == []
    assert read_gate_b_events(str(tmp_path / "also_missing.jsonl")) == []


def test_read_empty_file_returns_empty_list(tmp_path):
    sink = tmp_path / "gate_b_audit.jsonl"
    sink.write_text("", encoding="utf-8")
    assert read_gate_b_events(sink) == []


# ---------------------------------------------------------------------------
# asset_ids_from_ledger -- rows shaped like the real cricket ledger
#
# Real file: outputs/round3/phase0/other_cricket/licence_ledger.csv
# Header (verified): asset_id,scene_id,source,licence,kind,local_path,notes
# The first rows are the generated voice / music tracks; every stock
# row after them carries the provider's numeric id in ``asset_id``.
# ---------------------------------------------------------------------------

CRICKET_LEDGER_ROWS = [
    {
        "asset_id": "narration_voice",
        "scene_id": "all",
        "source": "own/generated (edge-tts synthesis)",
        "licence": "n/a — generated in-app",
        "kind": "voice",
        "local_path": "/pack/audio/narration.wav",
        "notes": "edge-tts voice ur-PK-AsadNeural; per-chapter sources: edge-tts",
    },
    {
        "asset_id": "music_bed",
        "scene_id": "all",
        "source": "own/generated (local tone bed)",
        "licence": "n/a — generated in-app",
        "kind": "music",
        "local_path": "/pack/audio/music_bed.wav",
        "notes": "generated in-app (sine + pink-noise bed); no external asset",
    },
    {
        "asset_id": "189845",
        "scene_id": "scene_0001",
        "source": "Pixabay stock video",
        "licence": "Pixabay Content License",
        "kind": "video",
        "local_path": "/pack/visuals/scene_0001_pixabay.mp4",
        "notes": "sha256=e8b52a25821c",
    },
    {
        "asset_id": "298628",
        "scene_id": "scene_0002",
        "source": "Pixabay stock video",
        "licence": "Pixabay Content License",
        "kind": "video",
        "local_path": "/pack/visuals/scene_0002_pixabay.mp4",
        "notes": "sha256=67fca22e18ed",
    },
    {
        "asset_id": "140210",
        "scene_id": "scene_0003",
        "source": "Pixabay stock video",
        "licence": "Pixabay Content License",
        "kind": "video",
        "local_path": "/pack/visuals/scene_0003_pixabay.mp4",
        "notes": "sha256=0770f9721e00",
    },
]


def test_asset_ids_from_ledger_real_cricket_shape():
    # The real stock IDs, in ledger order; voice/music contribute none.
    assert asset_ids_from_ledger(CRICKET_LEDGER_ROWS) == [
        "189845",
        "298628",
        "140210",
    ]


def test_asset_ids_from_ledger_skips_id_less_rows():
    rows = [
        # No asset_id key at all.
        {"scene_id": "scene_0001", "source": "Pixabay stock video", "kind": "video"},
        # Empty asset_id.
        {
            "asset_id": "",
            "scene_id": "scene_0002",
            "source": "Pixabay stock video",
            "kind": "video",
        },
        # Placeholder fallback, not a real provider id.
        {
            "asset_id": "unknown-stock-asset",
            "scene_id": "scene_0003",
            "source": "pixabay",
            "kind": "video",
        },
        # Generated rows by kind alone.
        {"asset_id": "some_voice", "kind": "voice", "source": "edge-tts"},
        {"asset_id": "some_music", "kind": "music", "source": "local"},
    ]
    assert asset_ids_from_ledger(rows) == []


def test_asset_ids_from_ledger_prefers_provider_asset_id():
    # Superset rows as built by app.integration.stock_ledger: the
    # provider id is explicit, asset_id carries the same value.
    rows = [
        {
            "asset_id": "326677",
            "provider_asset_id": "326677",
            "provider": "pixabay",
            "scene_id": "scene_0006",
            "source": "pixabay",
            "licence": "Pixabay Content License",
            "kind": "video",
            "local_path": "/pack/visuals/scene_0006_pixabay.mp4",
            "notes": "provider_asset_id=326677; licence=Pixabay Content License",
        },
        {
            # Only the provider id present -- still a real ID.
            "provider_asset_id": "91367",
            "scene_id": "scene_0005",
            "kind": "video",
        },
        {
            # Provider id only inside the notes evidence string.
            "scene_id": "scene_0007",
            "kind": "video",
            "notes": "provider_asset_id=216058; asset_page_url=https://example.invalid/x",
        },
    ]
    assert asset_ids_from_ledger(rows) == ["326677", "91367", "216058"]


def test_asset_ids_from_ledger_dedupes_and_handles_int_ids():
    rows = [
        {"asset_id": 189845, "kind": "video", "source": "Pixabay stock video"},
        {"asset_id": "189845", "kind": "video", "source": "Pixabay stock video"},
        {"asset_id": "298628", "kind": "video", "source": "Pixabay stock video"},
    ]
    assert asset_ids_from_ledger(rows) == ["189845", "298628"]
    assert asset_ids_from_ledger([]) == []
    assert asset_ids_from_ledger(None) == []
