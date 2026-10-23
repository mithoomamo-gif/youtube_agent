#!/usr/bin/env bash
# fetch_ffmpeg.sh — download the PINNED static FFmpeg build into
# packaging/vendor/ffmpeg/ (gitignored scratch; binaries are NEVER committed).
#
# PIN (Round 6 — change all four values together, and packaging/README.md):
#   FFmpeg version : n8.1.3-23-g5c4f5744cd  (FFmpeg 8.1 release branch)
#   Source         : BtbN/FFmpeg-Builds, GitHub release tag autobuild-2026-10-22-13-06
#   Linux asset    : ffmpeg-n8.1.3-23-g5c4f5744cd-linux64-gpl-8.1.tar.xz
#   SHA-256        : 1aa0c5526768bf4db3e62cba954169e3e0be38327c9cbbb6c5675794df6c03df
#   Windows asset  : ffmpeg-n8.1.3-23-g5c4f5744cd-win64-gpl-8.1.zip  (see fetch_ffmpeg.ps1)
#   SHA-256        : 75f5d6b673be7d93f59dcffb81a92c003580a7dcdbff768976b7907869aee09a
#
# LICENCE: this is BtbN's *GPL* build. FFmpeg itself is LGPL-2.1+, but this
# build is configured --enable-gpl with GPL components (libx264, libx265,
# …), so the combined binaries are licensed under the GPL-3.0. The archive's
# LICENSE.txt is copied into the vendor dir and MUST ship with the installer,
# together with the corresponding-source offer the GPL requires. Roadmap
# compliance note (MASTER_ROADMAP Part 11 / §T8 supply chain): the FFmpeg
# build is pinned + hashed, its version is recorded in the build manifest,
# and the shipped copy is signed by us or taken from the distributor —
# it is never an unpinned "latest" download.
set -euo pipefail

FFMPEG_VERSION="n8.1.3-23-g5c4f5744cd"
BTBN_TAG="autobuild-2026-10-22-13-06"
ASSET="ffmpeg-n8.1.3-23-g5c4f5744cd-linux64-gpl-8.1.tar.xz"
SHA256="1aa0c5526768bf4db3e62cba954169e3e0be38327c9cbbb6c5675794df6c03df"
URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/${BTBN_TAG}/${ASSET}"
LICENCE="GPL-3.0 (BtbN --enable-gpl build of FFmpeg; FFmpeg itself is LGPL-2.1+)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="${SCRIPT_DIR}/vendor/ffmpeg"
# Work on the same filesystem as the vendor dir, not /tmp: the archive is
# ~150 MB and /tmp is a small tmpfs on many CI/dev machines.
WORK_DIR="$(mktemp -d "${SCRIPT_DIR}/.fetch-tmp.XXXXXX")"
trap 'rm -rf "${WORK_DIR}"' EXIT

mkdir -p "${VENDOR_DIR}"

echo ">> Downloading ${URL}"
# Resumable + stall-tolerant: large release assets through proxies can hang
# at 0 bytes or drop mid-stream; -C - resumes instead of restarting.
curl -fL -C - --retry 10 --retry-all-errors --retry-delay 5 \
  --speed-limit 1024 --speed-time 60 \
  -o "${WORK_DIR}/${ASSET}" "${URL}"

echo ">> Verifying SHA-256"
if command -v sha256sum >/dev/null 2>&1; then
  echo "${SHA256}  ${WORK_DIR}/${ASSET}" | sha256sum -c -
else
  echo "${SHA256}  ${WORK_DIR}/${ASSET}" | shasum -a 256 -c -
fi

echo ">> Extracting"
# --no-same-owner: the archive carries the builder's uid/gid; a non-root
# fetcher cannot (and must not) reproduce them. Modes are set by install(1).
tar --no-same-owner -xJf "${WORK_DIR}/${ASSET}" -C "${WORK_DIR}"
SRC_DIR="$(find "${WORK_DIR}" -maxdepth 1 -type d -name 'ffmpeg-*' | head -n 1)"

install -m 0755 "${SRC_DIR}/bin/ffmpeg" "${VENDOR_DIR}/ffmpeg"
install -m 0755 "${SRC_DIR}/bin/ffprobe" "${VENDOR_DIR}/ffprobe"
# Licence text ships with the binaries (GPL requirement — see header).
cp "${SRC_DIR}/LICENSE.txt" "${VENDOR_DIR}/LICENSE.txt" 2>/dev/null || \
  find "${SRC_DIR}" -maxdepth 2 -iname 'LICEN*' -exec cp {} "${VENDOR_DIR}/" \; || true

cat > "${VENDOR_DIR}/VERSION.txt" <<EOF
ffmpeg_version=${FFMPEG_VERSION}
source_url=${URL}
sha256=${SHA256}
licence=${LICENCE}
fetched_by=packaging/fetch_ffmpeg.sh
EOF

echo ">> Verifying the fetched binary executes"
"${VENDOR_DIR}/ffmpeg" -hide_banner -version | head -n 1
if "${VENDOR_DIR}/ffmpeg" -hide_banner -filters 2>/dev/null | grep -qE '^\s+\S+\s+loudnorm\s'; then
  echo ">> loudnorm filter: PRESENT (pipeline requirement OK)"
else
  echo "!! loudnorm filter MISSING — this build cannot be shipped" >&2
  exit 1
fi
echo ">> Done: ${VENDOR_DIR}"
