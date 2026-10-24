"""Round 6 closure — ctx-less stage fallbacks must not use OS /tmp.

When a StageContext IS present, the voice/visuals stages write exactly
where the ctx says (unchanged). When it is absent (ctx=None, or a ctx
carrying none of the dir fields), the fallback work dir must resolve
under the canonical data dir's tmp area
(``<data dir>/tmp/voice`` / ``<data dir>/tmp/visuals``, via
``app.data_dir.DataDirLayout.tmp_dir``) — never a hard-coded
``/tmp/voice`` / ``/tmp/visuals``, which the packaged Windows app
cannot rely on.
"""

from __future__ import annotations

import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.integration import visuals_stage, voice_stage


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "yta-data"
    monkeypatch.setenv("YTA_DATA_DIR", str(root))
    return root


def test_voice_fallback_dir_is_data_dir_tmp(data_dir: Path):
    assert voice_stage._ctx_less_tmp_dir("voice") == data_dir / "tmp" / "voice"


def test_visuals_fallback_dir_is_data_dir_tmp(data_dir: Path):
    assert visuals_stage._ctx_less_tmp_dir("visuals") == (
        data_dir / "tmp" / "visuals"
    )


def test_voice_stage_ctx_less_writes_under_data_dir(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    captured: dict[str, Path] = {}

    def fake_render_chapter(index, text, *, audio_dir, **kwargs):
        captured["audio_dir"] = Path(audio_dir)
        wav_path = Path(audio_dir) / f"chapter_{index:02d}.wav"
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)  # 0.5 s of silence
        return wav_path, "stub-silence"

    monkeypatch.setattr(voice_stage, "_render_chapter", fake_render_chapter)

    bundle = {"chapters": [{"text": "Hello world, this is a fallback test."}]}
    result = voice_stage.synthesize_voice(bundle, None, None)

    expected = data_dir / "tmp" / "voice"
    assert captured["audio_dir"] == expected
    assert Path(result.narration_wav).is_relative_to(expected)
    # The forbidden location is the legacy hard-coded bare-temp stage
    # dir (<os-temp>/voice) — NOT any path that merely sits under a
    # /tmp-rooted pytest basetemp (CI's tmp_path lives under /tmp, and
    # the data dir under it is exactly where the write belongs).
    assert not Path(result.narration_wav).is_relative_to(
        Path(tempfile.gettempdir()) / "voice"
    )


def test_visuals_stage_ctx_less_writes_under_data_dir(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)

    def fake_render_plate(out_path, *, palette, seed, phrase, scene_class, hero):
        Image.new("RGB", (8, 8), palette[2]).save(out_path, format="PNG")

    monkeypatch.setattr(visuals_stage, "_render_plate", fake_render_plate)

    bundle = {
        "chapters": [{"narration": "A single short sentence for the fallback test."}],
        "niche_id": "facts",
    }
    voice = SimpleNamespace(
        duration_s=4.0, chapter_files=[], chapter_durations_s=[4.0]
    )
    result = visuals_stage.plan_and_generate_visuals(
        bundle, voice, None, None, use_stock=False
    )

    expected = data_dir / "tmp" / "visuals"
    assert (expected / "scene_manifest.json").is_file()
    assert result.scene_manifest, "expected at least one planned scene"
    for scene in result.scene_manifest:
        assert Path(scene["asset_path"]).is_relative_to(expected)
        # Same guard as the voice test: the legacy bare-temp stage dir
        # (<os-temp>/visuals) is forbidden; a /tmp-rooted pytest
        # basetemp holding the data dir is fine.
        assert not Path(scene["asset_path"]).is_relative_to(
            Path(tempfile.gettempdir()) / "visuals"
        )
