# `src-tauri/binaries/` — sidecar staging contract (CI)

This directory is a **build-input staging area**, not a source directory.
Its only committed file is this README. The executable staged here is
produced fresh by CI on a Windows runner for every build and is never
committed (see the `.gitignore` note at the bottom).

Roadmap: §11.1 (release artefacts), §11.2 (on-disk layout), §11.3
(inside-out signing).

## What CI must stage, exactly

Before `tauri build` runs, place the PyInstaller sidecar executable
here under its **target-triple name**:

```
src-tauri/binaries/agent-backend-x86_64-pc-windows-msvc.exe
```

- The name is not decoration: Tauri `externalBin` requires the binary
  at the declared path to carry the Rust target-triple suffix
  (`-x86_64-pc-windows-msvc`). `tauri.conf.json` declares the sidecar
  as `binaries/agent-backend`, and Tauri resolves + bundles the
  suffixed file, installing it next to the shell as
  `agent-backend.exe` (install layout: §11.2,
  `%LOCALAPPDATA%\Programs\YouTubeAgent\`).
- The sidecar is built with **PyInstaller `--onedir`, never
  `--onefile`** (§11.1: stable on-disk layout, faster starts, each
  file individually signable/scannable). What gets staged here is the
  `--onedir` build's executable under the exact name above.

## Signing: inside-out, before bundling (§11.3)

The staged `.exe` must be **Authenticode-signed BEFORE `tauri build`
bundles it** — SHA-256 file digest with an RFC 3161 timestamp
(`signtool sign /fd SHA256 /tr <timestamp-url> /td SHA256`), and
verified (`signtool verify /pa /all`) before the build proceeds.
A signature on the outer NSIS installer does **not** cover its
contents; signing the sidecar after bundling is not possible and
signing only the installer is a release-blocking defect.

## What does NOT belong here

- **`ffmpeg.exe` / `ffprobe.exe` are not shell `externalBin`.** They
  ship **inside the backend package** (the sidecar is their only
  caller, so they version with the backend — §11.5), not staged in
  this directory and not declared to the shell.
- No DLLs, test binaries, or local dev builds. If a file in this
  directory other than this README would be picked up by a build,
  it does not belong here.

## Never commit the staged binary

The staged `.exe` is a git-ignored build input: it is large,
machine-generated, Authenticode-signed with a per-release hash, and
worthless without the matching signed installer. Committing it would
pollute history and invite shipping an unsigned/stale sidecar.

> ⚠️ `.gitignore` status (Round 6, flagged for the updater/config
> worker who owns `.gitignore` this round): as of this writing the
> repo `.gitignore` does **not** yet contain a rule covering
> `src-tauri/binaries/*.exe`. Until that rule lands, be careful with
> `git add -A` on a machine that has staged a build — check
> `git status` and never force-add the `.exe`.
