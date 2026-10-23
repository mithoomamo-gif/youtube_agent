"""Tests for the FFmpeg/ffprobe resolution contract (Round 6).

Covers: resolution order (env override > bundled > PATH), the bundled
candidate layout (``sys.executable``-relative, incl. the roadmap Part 11
``runtime/`` layout), and the loudnorm capability probe (present and
absent, via a fixture runner for the absent case).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import ffmpeg_resolution as fr


def _touch_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _no_which(_name: str):
    return None


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------


def test_env_override_beats_bundled_and_path(tmp_path: Path) -> None:
    env_bin = _touch_exe(tmp_path / "custom" / "ffmpeg")
    _touch_exe(tmp_path / "anchor" / "runtime" / "ffmpeg")
    resolved = fr.resolve_ffmpeg(
        env={fr.FFMPEG_ENV_VAR: str(env_bin)},
        anchor=tmp_path / "anchor",
        which=lambda name: "/system/bin/ffmpeg",
    )
    assert resolved == env_bin


def test_bundled_beats_path(tmp_path: Path) -> None:
    bundled = _touch_exe(tmp_path / "anchor" / "runtime" / "ffmpeg")
    resolved = fr.resolve_ffmpeg(
        env={}, anchor=tmp_path / "anchor", which=lambda name: "/system/bin/ffmpeg"
    )
    assert resolved == bundled


def test_bundled_flat_and_parent_runtime_layouts(tmp_path: Path) -> None:
    flat = _touch_exe(tmp_path / "a" / "ffprobe")
    assert (
        fr.resolve_ffprobe(env={}, anchor=tmp_path / "a", which=_no_which) == flat
    )
    # <anchor>/../../runtime — versioned backend-release layout: sidecar in
    # releases/<ver>/, shared runtime/ at the install root.
    root_runtime = _touch_exe(tmp_path / "runtime" / "ffprobe")
    nested_anchor = tmp_path / "releases" / "1.0"
    nested_anchor.mkdir(parents=True)
    assert (
        fr.resolve_ffprobe(env={}, anchor=nested_anchor, which=_no_which)
        == root_runtime
    )
    # <anchor>/../runtime also resolves (one level up).
    parent_runtime = _touch_exe(tmp_path / "releases" / "runtime" / "ffmpeg")
    assert (
        fr.resolve_ffmpeg(env={}, anchor=nested_anchor, which=_no_which)
        == parent_runtime
    )


def test_path_is_last_resort(tmp_path: Path) -> None:
    resolved = fr.resolve_ffmpeg(
        env={}, anchor=tmp_path / "empty", which=lambda name: "/system/bin/ffmpeg"
    )
    assert resolved == Path("/system/bin/ffmpeg")


def test_nothing_resolves_returns_none(tmp_path: Path) -> None:
    assert (
        fr.resolve_ffmpeg(env={}, anchor=tmp_path / "empty", which=_no_which)
        is None
    )


def test_missing_env_override_falls_through_to_bundled(tmp_path: Path) -> None:
    bundled = _touch_exe(tmp_path / "anchor" / "runtime" / "ffmpeg")
    resolved = fr.resolve_ffmpeg(
        env={fr.FFMPEG_ENV_VAR: str(tmp_path / "does-not-exist" / "ffmpeg")},
        anchor=tmp_path / "anchor",
        which=_no_which,
    )
    assert resolved == bundled


def test_ffprobe_uses_its_own_env_var(tmp_path: Path) -> None:
    probe_bin = _touch_exe(tmp_path / "custom" / "ffprobe")
    resolved = fr.resolve_ffprobe(
        env={fr.FFPROBE_ENV_VAR: str(probe_bin)},
        anchor=tmp_path / "anchor",
        which=_no_which,
    )
    assert resolved == probe_bin


def test_windows_bundled_candidate_names(tmp_path: Path) -> None:
    candidates = fr.bundled_candidates(
        "ffmpeg", anchor=tmp_path / "anchor", platform="win32"
    )
    assert candidates[0] == tmp_path / "anchor" / "runtime" / "ffmpeg.exe"
    assert candidates[1] == tmp_path / "anchor" / "ffmpeg.exe"


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValueError):
        fr.resolve_binary("ffplay", env={}, which=_no_which)


# ---------------------------------------------------------------------------
# Loudnorm probe
# ---------------------------------------------------------------------------

_FILTERS_WITH_LOUDNORM = """Filters:
  T.. = Timeline support
      ...
 .. aeval            A->A       Filter audio signal according to a JavaScript expression.
 .. loudnorm         A->A       EBU R128 loudness normalization
 .. volume           A->A       Change input volume.
"""

_FILTERS_WITHOUT_LOUDNORM = """Filters:
  T.. = Timeline support
      ...
 .. aeval            A->A       Filter audio signal according to a JavaScript expression.
 .. volume           A->A       Change input volume.
"""


def _fake_runner(filters_output: str):
    def runner(cmd, **kwargs):
        if "-version" in cmd:
            return SimpleNamespace(
                stdout="ffmpeg version 8.1.3-test Copyright (c) the FFmpeg developers\n",
                stderr="",
                returncode=0,
            )
        return SimpleNamespace(stdout=filters_output, stderr="", returncode=0)

    return runner


def test_has_loudnorm_parser() -> None:
    assert fr.has_loudnorm(_FILTERS_WITH_LOUDNORM) is True
    assert fr.has_loudnorm(_FILTERS_WITHOUT_LOUDNORM) is False
    assert "loudnorm" in fr.parse_filter_names(_FILTERS_WITH_LOUDNORM)


def test_probe_detects_loudnorm_present(tmp_path: Path) -> None:
    binary = _touch_exe(tmp_path / "ffmpeg")
    result = fr.probe_loudnorm(binary, runner=_fake_runner(_FILTERS_WITH_LOUDNORM))
    assert result.found is True
    assert result.loudnorm_present is True
    assert result.version and result.version.startswith("ffmpeg version 8.1.3-test")
    assert result.error is None


def test_probe_detects_loudnorm_absent(tmp_path: Path) -> None:
    binary = _touch_exe(tmp_path / "ffmpeg")
    result = fr.probe_loudnorm(
        binary, runner=_fake_runner(_FILTERS_WITHOUT_LOUDNORM)
    )
    assert result.found is True
    assert result.loudnorm_present is False


def test_probe_when_ffmpeg_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr, "resolve_ffmpeg", lambda: None)
    result = fr.probe_loudnorm()
    assert result.ffmpeg is None
    assert result.found is False
    assert result.loudnorm_present is False
    assert result.error and "not resolved" in result.error


def test_probe_execution_failure_is_recorded(tmp_path: Path) -> None:
    binary = _touch_exe(tmp_path / "ffmpeg")

    def boom(cmd, **kwargs):
        raise FileNotFoundError("no such file")

    result = fr.probe_loudnorm(binary, runner=boom)
    assert result.found is False
    assert result.loudnorm_present is False
    assert result.error and "execution failed" in result.error


# ---------------------------------------------------------------------------
# Frozen-like layout: bundled binary found with PATH stripped
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="system ffmpeg needed as the staged binary"
)
def test_frozen_layout_bundled_binary_resolves_and_probes(tmp_path: Path) -> None:
    """Simulate the frozen install: sidecar anchor + runtime/ffmpeg, no PATH."""
    anchor = tmp_path / "YouTubeAgent"
    runtime_dir = anchor / "runtime"
    runtime_dir.mkdir(parents=True)
    real = Path(shutil.which("ffmpeg"))
    staged = runtime_dir / real.name
    try:
        staged.symlink_to(real)
    except OSError:
        shutil.copy2(real, staged)

    resolved = fr.resolve_ffmpeg(env={}, anchor=anchor, which=_no_which)
    assert resolved == staged

    # And the staged binary really executes + carries loudnorm.
    result = fr.probe_loudnorm(resolved)
    assert result.found is True
    assert result.loudnorm_present is True
    proc = subprocess.run(
        [str(resolved), "-hide_banner", "-version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.startswith("ffmpeg version")


def test_real_system_ffmpeg_probe_if_present() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    result = fr.probe_loudnorm()
    assert result.found is True
    # The dev/CI ffmpeg is a full build; loudnorm is a pipeline invariant.
    assert result.loudnorm_present is True
    assert sys.platform  # probe ran on this platform without raising
