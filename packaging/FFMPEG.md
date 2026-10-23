# Packaging — FFmpeg bundling & the runtime resolution contract

Round 6 (Sidecar Freeze + Media Bundling). This is where the shell /
packaging workers find the FFmpeg contract. The backend side of the
contract lives in `backend/app/ffmpeg_resolution.py` (its module
docstring is authoritative for resolution behaviour).

## The pinned build

| | |
|---|---|
| FFmpeg version | `n8.1.3-23-g5c4f5744cd` (FFmpeg 8.1 release branch) |
| Source | BtbN/FFmpeg-Builds, GitHub release tag `autobuild-2026-10-22-13-06` |
| Linux asset | `ffmpeg-n8.1.3-23-g5c4f5744cd-linux64-gpl-8.1.tar.xz` |
| Linux SHA-256 | `1aa0c5526768bf4db3e62cba954169e3e0be38327c9cbbb6c5675794df6c03df` |
| Windows asset | `ffmpeg-n8.1.3-23-g5c4f5744cd-win64-gpl-8.1.zip` |
| Windows SHA-256 | `75f5d6b673be7d93f59dcffb81a92c003580a7dcdbff768976b7907869aee09a` |
| Linux URL | `https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-22-13-06/ffmpeg-n8.1.3-23-g5c4f5744cd-linux64-gpl-8.1.tar.xz` |
| Windows URL | `https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-22-13-06/ffmpeg-n8.1.3-23-g5c4f5744cd-win64-gpl-8.1.zip` |

Fetch with `fetch_ffmpeg.sh` (Linux shell; verifies SHA-256, stages
binaries + licence into `vendor/ffmpeg/`, smoke-tests `-version` and
the `loudnorm` filter) or `fetch_ffmpeg.ps1` (Windows CI; same pin,
hash and checks). `vendor/` is gitignored scratch — **never commit
the binaries**.

**Licence.** This is BtbN's *GPL* build: FFmpeg itself is LGPL-2.1+,
but the build is `--enable-gpl` with GPL components (libx264, libx265,
…), so the combined binaries are **GPL-3.0**. The archive's
`LICENSE.txt` is staged alongside the binaries and must ship in the
installer together with the GPL corresponding-source offer.
**Roadmap compliance note** (MASTER_ROADMAP Part 11 + supply-chain
§T8): FFmpeg ships as a *pinned upstream build with its version
recorded* and its *hash pinned in the build manifest*; the shipped
copy is signed by us or taken distributor-signed; the Windows build
must be the full-featured one (hardware encoders `h264_nvenc` /
`h264_qsv` / `h264_amf` are compile-time features of this GPL build)
and first-run detection verifies what is actually present.

## Runtime resolution contract (backend)

`backend/app/ffmpeg_resolution.py` is the ONE resolver. Order, per
binary (`ffmpeg`, `ffprobe`):

1. **Env override** — `YTA_FFMPEG_PATH` / `YTA_FFPROBE_PATH` (full
   path). The roadmap names no variable for this; the names extend the
   `YTA_*` launch-environment namespace in `backend/app/config.py`.
   A set-but-missing override is logged (WARNING) and ignored.
2. **Bundled, `sys.executable`-relative** (never CWD-relative), for
   anchor = the sidecar executable's directory:
   `runtime/<name>`, then `<name>`, then `../runtime/<name>`, then
   `../../runtime/<name>` (`.exe` suffix on Windows).
3. **System PATH** via `shutil.which`.

**Packaging action:** stage the fetched binaries into the `runtime\`
directory beside `agent-backend.exe` — the roadmap Part 11 install
layout (`%LOCALAPPDATA%\Programs\YouTubeAgent\agent-backend.exe` +
`runtime\ffmpeg.exe` + `runtime\ffprobe.exe`). No PATH mutation, no
registry entries, no working-directory assumptions. (The PyInstaller
spec intentionally does NOT embed FFmpeg — see
`yta_sidecar.spec`; the binaries version with the backend package,
per Part 11, and are staged next to the sidecar instead.)

## Loudnorm capability probe

The pipeline loudness-normalises to −14 LUFS with the `loudnorm`
filter (roadmap Part 6.7), so the build's filter list is a hard
requirement. `app.ffmpeg_resolution.probe_loudnorm()` runs the
*resolved* ffmpeg's `-filters`, confirms `loudnorm`, and logs/records
the outcome (`LoudnormProbeResult`: binary, version line, present /
absent, error). Run it at first run / smoke-test time; the fetch
scripts above perform the same check at fetch time.
