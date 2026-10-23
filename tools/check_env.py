#!/usr/bin/env python3
"""
check_env.py — cross-platform dev-environment check for YouTube Agent.

Reports Python, Node, npm, cargo/rustc, ffmpeg and git, prints a
✓ / ✗ / ⚠ table, and exits 0 unless a hard requirement fails.
The only hard requirement is Python >= 3.12 (the backend runtime).
Everything else is a warning: e.g. cargo/rustc are only needed to build
the Tauri shell, and this dev VM intentionally has no Rust toolchain.

Usage:
    python3 tools/check_env.py
    python tools/check_env.py    # Windows
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys

# Make the ✓/✗/⚠ glyphs safe on Windows consoles with legacy codepages.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

MIN_PYTHON = (3, 12)
RECOMMENDED_NODE_MAJOR = 20  # project built with Node 24; 20+ is fine


def run_version(cmd: list[str]) -> str | None:
    """Run a --version/-version style command; return first line or None."""
    exe = shutil.which(cmd[0])
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, *cmd[1:]],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    text = (out.stdout or out.stderr or "").strip()
    if not text:
        return None
    return text.splitlines()[0].strip()


def parse_major(version_text: str | None) -> int | None:
    if not version_text:
        return None
    # Handles "v24.20.0", "node v24...", "ffmpeg version 8.1.2", "git version 2.43.0"
    import re

    m = re.search(r"(\d+)\.(\d+)", version_text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def main() -> int:
    rows: list[tuple[str, str, str]] = []  # (status glyph, tool, detail)
    hard_fail = False

    # --- Python (hard requirement) -------------------------------------------
    py_ver = sys.version_info
    py_str = f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}"
    if py_ver >= MIN_PYTHON:
        rows.append(("✓", "Python", f"{py_str} (>= 3.12 required) — {sys.executable}"))
    else:
        rows.append(("✗", "Python", f"{py_str} — NEED >= 3.12. Install Python 3.12+ and re-run."))
        hard_fail = True

    # --- Node -----------------------------------------------------------------
    node_ver = run_version(["node", "--version"])
    if node_ver:
        major = parse_major(node_ver)
        if major is not None and major >= RECOMMENDED_NODE_MAJOR:
            rows.append(("✓", "Node", f"{node_ver} (>= {RECOMMENDED_NODE_MAJOR} recommended)"))
        else:
            rows.append(("⚠", "Node", f"{node_ver} — project built with Node 24; 20+ recommended"))
    else:
        rows.append(("⚠", "Node", "not found — needed for the React/Vite frontend (src/)"))

    # --- npm ------------------------------------------------------------------
    npm_ver = run_version(["npm", "--version"])
    if npm_ver:
        rows.append(("✓", "npm", npm_ver))
    else:
        rows.append(("⚠", "npm", "not found — usually ships with Node.js; needed for frontend deps"))

    # --- cargo / rustc (Tauri shell only) --------------------------------------
    cargo_ver = run_version(["cargo", "--version"])
    rustc_ver = run_version(["rustc", "--version"])
    if cargo_ver and rustc_ver:
        rows.append(("✓", "cargo", cargo_ver))
        rows.append(("✓", "rustc", rustc_ver))
    elif cargo_ver or rustc_ver:
        rows.append(("⚠", "cargo/rustc", f"partial install — cargo: {cargo_ver or 'missing'}, rustc: {rustc_ver or 'missing'}"))
    else:
        rows.append((
            "⚠",
            "cargo/rustc",
            "not found — needed ONLY to build/run the Tauri shell (src-tauri/). "
            "Backend + frontend dev work fine without it. Install via https://rustup.rs",
        ))

    # --- ffmpeg ---------------------------------------------------------------
    ffmpeg_ver = run_version(["ffmpeg", "-version"])
    if ffmpeg_ver:
        # "ffmpeg version 8.1.2 Copyright..." -> keep the version token
        detail = ffmpeg_ver
        rows.append(("✓", "ffmpeg", detail))
    else:
        rows.append(("⚠", "ffmpeg", "not found — needed for video assembly / thumbnails at runtime"))

    # --- git ------------------------------------------------------------------
    git_ver = run_version(["git", "--version"])
    if git_ver:
        rows.append(("✓", "git", git_ver))
    else:
        rows.append(("⚠", "git", "not found — recommended for version control"))

    # --- Report -----------------------------------------------------------------
    print("YouTube Agent — dev environment check")
    print(f"Platform: {platform.system()} {platform.release()} ({platform.machine()})")
    print("")
    # Simple aligned table
    width = max(len(r[1]) for r in rows) if rows else 10
    for glyph, tool, detail in rows:
        print(f" {glyph}  {tool:<{width}}  {detail}")
    print("")
    if hard_fail:
        print("✗ Hard requirement failed (Python >= 3.12). Fix the ✗ item above and re-run.")
        return 1
    warnings = sum(1 for r in rows if r[0] == "⚠")
    if warnings:
        print(f"✓ Ready for backend/frontend dev ({warnings} warning(s) above — see notes, usually Tauri/Rust only).")
    else:
        print("✓ All checks passed — fully ready, including the Tauri shell.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
