"""Fixtures for integration glue tests: fake stage results.

These fakes mirror the shared Round-2 contract shapes (attribute
access, like the real pydantic/dataclass stage results) without any
network, TTS or ffmpeg work.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: real end-to-end integration run (deselected in fast runs)"
    )


def fake_bundle(**overrides):
    data = dict(
        title="Test Title: Ocean Secrets",
        hook="A hook that is definitely long enough to matter.",
        chapters=[SimpleNamespace(index=1, title="Hook", text="Chapter text.")],
        script_text="Chapter text.",
        narration_text="Chapter text.",
        thumbnail_text="Ocean Secrets",
        description="A test description.",
        tags=["test", "ocean"],
        claims=[],
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def fake_voice(**overrides):
    data = dict(
        narration_wav="/nonexistent/narration.wav",
        duration_s=55.0,
        path_used="stub",
        voice_id="stub-voice",
        chapter_files=[],
        music_wav=None,
        mixed_master_wav="/nonexistent/master.wav",
        ledger_rows=[
            {
                "asset_id": "voice-1", "scene_id": "all",
                "source": "own/generated: stub voice", "licence": "own work",
                "kind": "voice", "local_path": "/nonexistent/narration.wav",
                "notes": "stub",
            }
        ],
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def fake_visuals(**overrides):
    data = dict(
        scene_manifest=[],
        asset_files=[],
        ledger_rows=[
            {
                "asset_id": "plate-1", "scene_id": "1",
                "source": "own/generated: PIL plate", "licence_name": "own work",
                "kind": "image", "local_path": "/nonexistent/plate.png",
                "notes": "",
            }
        ],
        total_duration_s=55.0,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def fake_assemble(tmp_path, **overrides):
    final = tmp_path / "final.mp4"
    final.write_bytes(b"fake-mp4")
    srt = tmp_path / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    thumbs = []
    for i in range(3):
        t = tmp_path / f"thumbnail_{i + 1}.png"
        t.write_bytes(b"fake-png")
        thumbs.append(str(t))
    data = dict(
        final_mp4=str(final),
        duration_s=55.0,
        captions_srt=str(srt),
        subtitle_path_used="whisper-stub",
        thumbnails=thumbs,
        qc_passed=True,
        qc_summary="all good",
        loudness_lufs=-14.2,
        render_path_used="ffmpeg-local",
        ledger_rows=[],
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def fake_publish(**overrides):
    data = dict(
        gate_b_passed=True,
        manifest={"video_id": "fake-1", "state": "ready"},
        manifest_path=None,
        publish_state="ready_for_review",
        tokens=[],
    )
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.fixture()
def stage_fakes(tmp_path):
    return SimpleNamespace(
        bundle=fake_bundle(),
        voice=fake_voice(),
        visuals=fake_visuals(),
        assemble=fake_assemble(tmp_path),
        publish=fake_publish(),
    )
