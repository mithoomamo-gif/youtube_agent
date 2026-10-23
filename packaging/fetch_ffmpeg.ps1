# fetch_ffmpeg.ps1 — Windows CI counterpart of fetch_ffmpeg.sh.
# Downloads the PINNED static FFmpeg build into packaging\vendor\ffmpeg\
# (gitignored scratch; binaries are NEVER committed).
# Written for Windows CI — not executed on the Linux dev VM.
#
# PIN (Round 6 — keep in lockstep with fetch_ffmpeg.sh + packaging/README.md):
#   FFmpeg version : n8.1.3-23-g5c4f5744cd  (FFmpeg 8.1 release branch)
#   Source         : BtbN/FFmpeg-Builds, GitHub release tag autobuild-2026-10-22-13-06
#   Windows asset  : ffmpeg-n8.1.3-23-g5c4f5744cd-win64-gpl-8.1.zip
#   SHA-256        : 75f5d6b673be7d93f59dcffb81a92c003580a7dcdbff768976b7907869aee09a
#
# LICENCE: BtbN *GPL* build. FFmpeg itself is LGPL-2.1+, but this build is
# configured --enable-gpl with GPL components (libx264, libx265, ...), so the
# combined binaries are GPL-3.0. LICENSE.txt from the archive is copied into
# the vendor dir and MUST ship with the installer, together with the
# corresponding-source offer the GPL requires. Roadmap compliance note
# (MASTER_ROADMAP Part 11 / supply-chain T8): pinned + hashed build, version
# recorded in the build manifest, shipped copy signed by us or taken from
# the distributor — never an unpinned "latest" download. The win64 GPL build
# carries the compile-time hardware encoders the roadmap requires
# (h264_nvenc / h264_qsv / h264_amf); first-run detection verifies them.
$ErrorActionPreference = 'Stop'

$FfmpegVersion = 'n8.1.3-23-g5c4f5744cd'
$BtbnTag       = 'autobuild-2026-10-22-13-06'
$Asset         = 'ffmpeg-n8.1.3-23-g5c4f5744cd-win64-gpl-8.1.zip'
$Sha256        = '75f5d6b673be7d93f59dcffb81a92c003580a7dcdbff768976b7907869aee09a'
$Url           = "https://github.com/BtbN/FFmpeg-Builds/releases/download/$BtbnTag/$Asset"
$Licence       = 'GPL-3.0 (BtbN --enable-gpl build of FFmpeg; FFmpeg itself is LGPL-2.1+)'

$VendorDir = Join-Path $PSScriptRoot 'vendor\ffmpeg'
$WorkDir   = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Force -Path $VendorDir, $WorkDir | Out-Null

try {
    $ZipPath = Join-Path $WorkDir $Asset
    Write-Host ">> Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $ZipPath

    Write-Host '>> Verifying SHA-256'
    $Actual = (Get-FileHash -Algorithm SHA256 -Path $ZipPath).Hash.ToLower()
    if ($Actual -ne $Sha256) { throw "SHA-256 mismatch: got $Actual, want $Sha256" }

    Write-Host '>> Extracting'
    Expand-Archive -Path $ZipPath -DestinationPath $WorkDir -Force
    $SrcDir = Get-ChildItem -Path $WorkDir -Directory -Filter 'ffmpeg-*' | Select-Object -First 1

    Copy-Item (Join-Path $SrcDir.FullName 'bin\ffmpeg.exe')  (Join-Path $VendorDir 'ffmpeg.exe')  -Force
    Copy-Item (Join-Path $SrcDir.FullName 'bin\ffprobe.exe') (Join-Path $VendorDir 'ffprobe.exe') -Force
    # Licence text ships with the binaries (GPL requirement — see header).
    Get-ChildItem -Path $SrcDir.FullName -Filter 'LICEN*' -Recurse -Depth 1 |
        ForEach-Object { Copy-Item $_.FullName $VendorDir -Force }

    @(
        "ffmpeg_version=$FfmpegVersion"
        "source_url=$Url"
        "sha256=$Sha256"
        "licence=$Licence"
        'fetched_by=packaging/fetch_ffmpeg.ps1'
    ) | Set-Content -Path (Join-Path $VendorDir 'VERSION.txt') -Encoding UTF8

    Write-Host '>> Verifying the fetched binary executes'
    & (Join-Path $VendorDir 'ffmpeg.exe') -hide_banner -version | Select-Object -First 1
    $Filters = & (Join-Path $VendorDir 'ffmpeg.exe') -hide_banner -filters 2>$null
    if ($Filters -match '(?m)^\s+\S+\s+loudnorm\s') {
        Write-Host '>> loudnorm filter: PRESENT (pipeline requirement OK)'
    } else {
        throw 'loudnorm filter MISSING — this build cannot be shipped'
    }
    Write-Host ">> Done: $VendorDir"
}
finally {
    Remove-Item -Recurse -Force $WorkDir -ErrorAction SilentlyContinue
}
