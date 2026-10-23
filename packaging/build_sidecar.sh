#!/usr/bin/env bash
# packaging/build_sidecar.sh — YouTube Agent sidecar freeze (Linux/macOS).
#
# Mirror of packaging/build_sidecar.ps1 (the Windows/CI entry point that
# .github/workflows/release.yml invokes); the two scripts implement the
# SAME steps. This .sh is the Linux dev/VM entry point and is what the
# Round 6 proof build on this machine was produced with.
#
# What it does, in order:
#   1. Resolves the release version from the version of record (below)
#      and verifies every mirror file agrees — a mismatch fails loudly.
#   2. Cleans the previous freeze output (packaging/dist-sidecar/ and
#      packaging/build-sidecar/ only — both are build artefacts).
#   3. Runs PyInstaller against packaging/yta_sidecar.spec (--onedir,
#      per roadmap Part 11 / docs/BUILD.md section 2; --onefile is
#      non-negotiable NO). PYTHONPATH is pointed at backend/ for the
#      freeze: the spec collects the app package with
#      collect_submodules("app") at spec-exec time, when PyInstaller
#      has NOT yet applied the spec's pathex — without PYTHONPATH the
#      collection silently returns ZERO modules and every module the
#      app loads by string at runtime (app.routers.health et al.) is
#      missing from the bundle. A post-freeze completeness gate
#      (step 4b) fails loudly if that ever regresses. The permanent
#      home for this fix is the spec itself (insert BACKEND into
#      sys.path before collecting); until the spec owner lands it,
#      these scripts carry the workaround.
#   4. Performs the spec's hazard-H1 copy step: prompts/ is placed
#      BESIDE the executable (bundle root), because the frozen code
#      resolves it as parents[3] / "prompts" of app/agents — the spec
#      also bundles a mirror at _internal/prompts, but the bundle-root
#      copy is the one the frozen code actually reads.
#   5. Stamps the version into the bundle: <bundle>/VERSION (plain
#      text) and <bundle>/build-info.json (version, git commit, build
#      time, toolchain). The Windows exe version RESOURCE is NOT set
#      here: PyInstaller takes it from a version= file in the spec,
#      which is the spec owner's file — the spec process is given
#      YTA_VERSION / YTA_GIT_COMMIT / YTA_BUILD_TIME in its environment
#      so a future spec revision can stamp the resource from the same
#      values these scripts stamp into the files.
#   6. Stages the frozen tree for the Tauri shell under
#      src-tauri/binaries/ (externalBin layout, see below).
#   7. Prints the final bundle path + total size.
#
# Version single source of truth:
#   docs/RELEASE_RUNBOOK.md section 2 designates src-tauri/tauri.conf.json
#   (top-level "version") as the release version of record, with
#   Cargo.toml, package.json, backend/app/__init__.py (__version__) and
#   backend/pyproject.toml as mirrors that must agree (packaging/
#   preflight.sh checks the same set). tauri.conf.json currently carries
#   the Tauri path reference "../package.json", i.e. it delegates the
#   literal value to package.json — this script follows that reference.
#   There is deliberately NO separate packaging/VERSION file: the
#   runbook locks "no single version file; a single version value", and
#   a sixth copy would be one more mirror to drift.
#   The CI environment variable RELEASE_VERSION (set by release.yml
#   from the git tag / workflow input) overrides the STAMPED version
#   only; the mirror-consistency check always runs against the record,
#   and a RELEASE_VERSION that disagrees with the record warns loudly
#   (the runbook bump procedure commits the bump before tagging, so
#   they are expected to match).
#
# Python: backend/.venv/bin/python when present (the layout the spec
# header documents), else python3 on PATH. PyInstaller: if the chosen
# interpreter cannot run it, a pinned PyInstaller (>=6.11,<7) is
# installed into that interpreter's environment first.
#
# Tauri staging layout (src-tauri/binaries/):
#   agent-backend-<target-triple>   the sidecar exe under the exact
#                                   externalBin name tauri.conf.json
#                                   expects (suffix .exe on Windows;
#                                   the rename itself is Tauri's at
#                                   bundle time — this is the pre-stage)
#   agent-backend/                  the complete --onedir tree
#                                   (_internal/, prompts/, stamps), so
#                                   the exe's siblings travel with it
#
# Usage:
#   ./packaging/build_sidecar.sh
#
# Idempotent: every run cleans and rebuilds from scratch. Fails loudly
# (non-zero exit, message on stderr) on any error. No network access
# except the one-time PyInstaller install fallback above. No secrets
# are read or printed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

SPEC_FILE="$REPO_ROOT/packaging/yta_sidecar.spec"
DIST_ROOT="$REPO_ROOT/packaging/dist-sidecar"
WORK_ROOT="$REPO_ROOT/packaging/build-sidecar"
BUNDLE_NAME="yta_sidecar"
BUNDLE_DIR="$DIST_ROOT/$BUNDLE_NAME"
STAGE_DIR="$REPO_ROOT/src-tauri/binaries"
PYINSTALLER_PIN="pyinstaller>=6.11,<7"

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

info() {
    printf '==> %s\n' "$*"
}

warn() {
    printf 'WARNING: %s\n' "$*" >&2
}

# ---------------------------------------------------------------------------
# 0. Interpreter
# ---------------------------------------------------------------------------
if [ -x "$REPO_ROOT/backend/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/backend/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    fail "no Python interpreter found (looked for backend/.venv/bin/python, then python3 on PATH)"
fi
info "Python: $PYTHON ($("$PYTHON" --version 2>&1))"

# ---------------------------------------------------------------------------
# 1. Version: resolve from the version of record + verify all mirrors.
#    Extraction patterns intentionally match packaging/preflight.sh.
# ---------------------------------------------------------------------------
tauri_version_field() {
    # Print the raw top-level "version" string of tauri.conf.json.
    "$PYTHON" - "$REPO_ROOT/src-tauri/tauri.conf.json" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.load(fh).get("version", ""))
except Exception as exc:  # noqa: BLE001 - surfaced by the caller
    print("", file=sys.stdout)
    print("could not parse src-tauri/tauri.conf.json: %s" % exc, file=sys.stderr)
    sys.exit(1)
PYEOF
}

json_version_field() {
    # Print the top-level "version" string of an arbitrary JSON file.
    "$PYTHON" - "$1" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.load(fh).get("version", ""))
except Exception as exc:  # noqa: BLE001
    print("could not parse %s: %s" % (sys.argv[1], exc), file=sys.stderr)
    sys.exit(1)
PYEOF
}

sed_version() {
    # $1 = file, $2 = sed pattern with one capture group. Prints "" if absent.
    [ -f "$1" ] || return 0
    sed -n "s/$2/\\1/p" "$1" | head -n 1
}

TAURI_CONF="$REPO_ROOT/src-tauri/tauri.conf.json"
[ -f "$TAURI_CONF" ] || fail "src-tauri/tauri.conf.json not found under $REPO_ROOT"

RAW_RECORD="$(tauri_version_field)" || fail "could not read the version of record from src-tauri/tauri.conf.json"
[ -n "$RAW_RECORD" ] || fail "src-tauri/tauri.conf.json has no top-level \"version\""

case "$RAW_RECORD" in
    *.json)
        # Tauri path reference (current tree: "../package.json") — the
        # referenced file carries the literal version of record.
        RECORD_FILE="$(cd "$REPO_ROOT/src-tauri" && cd "$(dirname "$RAW_RECORD")" && pwd)/$(basename "$RAW_RECORD")"
        [ -f "$RECORD_FILE" ] || fail "tauri.conf.json version references $RAW_RECORD, which does not resolve to a file"
        RECORD_VERSION="$(json_version_field "$RECORD_FILE")" || fail "could not read version from $RECORD_FILE"
        RECORD_DESC="src-tauri/tauri.conf.json -> $RAW_RECORD"
        ;;
    *)
        RECORD_VERSION="$RAW_RECORD"
        RECORD_DESC="src-tauri/tauri.conf.json"
        ;;
esac
[ -n "$RECORD_VERSION" ] || fail "version of record resolved to an empty string ($RECORD_DESC)"

printf '%s\n' "$RECORD_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$' \
    || fail "version of record '$RECORD_VERSION' ($RECORD_DESC) is not semver X.Y.Z"

MISMATCHES=""
check_mirror() {
    # $1 = label, $2 = value found ("" = missing)
    if [ "$2" != "$RECORD_VERSION" ]; then
        MISMATCHES="${MISMATCHES} ${1}='${2:-<missing>}'"
    fi
}
check_mirror "package.json"            "$(json_version_field "$REPO_ROOT/package.json" 2>/dev/null || true)"
check_mirror "src-tauri/Cargo.toml"    "$(sed_version "$REPO_ROOT/src-tauri/Cargo.toml" '^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*')"
check_mirror "backend/app/__init__.py" "$(sed_version "$REPO_ROOT/backend/app/__init__.py" '^[[:space:]]*__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*')"
check_mirror "backend/pyproject.toml"  "$(sed_version "$REPO_ROOT/backend/pyproject.toml" '^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*')"
if [ -n "$MISMATCHES" ]; then
    fail "version drift: version of record ($RECORD_DESC) = '$RECORD_VERSION' but mismatched mirror(s):$MISMATCHES — fix the lagging file(s) (docs/RELEASE_RUNBOOK.md section 2), never build over a mismatch"
fi
info "Version of record: $RECORD_VERSION ($RECORD_DESC) — all mirrors agree"

STAMP_VERSION="$RECORD_VERSION"
if [ -n "${RELEASE_VERSION:-}" ]; then
    STAMP_VERSION="${RELEASE_VERSION#v}"
    if [ "$STAMP_VERSION" != "$RECORD_VERSION" ]; then
        warn "RELEASE_VERSION='$STAMP_VERSION' differs from the version of record '$RECORD_VERSION'; stamping '$STAMP_VERSION' (CI tag/input wins for the stamp, per release.yml). The mirror check above still passed against the record."
    fi
    info "Stamp version (from RELEASE_VERSION): $STAMP_VERSION"
fi

# ---------------------------------------------------------------------------
# 2. Spec + PyInstaller prerequisites
# ---------------------------------------------------------------------------
[ -f "$SPEC_FILE" ] || fail "spec not found: packaging/yta_sidecar.spec (owned by the sidecar-freeze spec worker; there is nothing to freeze without it)"

if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
    info "PyInstaller not available in this interpreter — installing pinned '$PYINSTALLER_PIN'"
    "$PYTHON" -m pip install "$PYINSTALLER_PIN" || fail "pip install of PyInstaller failed"
fi
PYINSTALLER_VERSION="$("$PYTHON" -m PyInstaller --version 2>&1 | head -n 1)"
info "PyInstaller: $PYINSTALLER_VERSION"

GIT_COMMIT="unknown"
if command -v git >/dev/null 2>&1 && git -C "$REPO_ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
    GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
fi
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PLATFORM="$("$PYTHON" -c 'import platform; print(platform.system() + "-" + platform.machine())')"

# ---------------------------------------------------------------------------
# 3. Clean previous freeze output (artefact dirs only)
# ---------------------------------------------------------------------------
info "Cleaning previous freeze output: packaging/dist-sidecar/, packaging/build-sidecar/"
rm -rf "$DIST_ROOT" "$WORK_ROOT"
mkdir -p "$DIST_ROOT" "$WORK_ROOT"

# ---------------------------------------------------------------------------
# 4. Freeze
# ---------------------------------------------------------------------------
info "Freezing sidecar: PyInstaller packaging/yta_sidecar.spec (--onedir)"
YTA_VERSION="$STAMP_VERSION" \
YTA_GIT_COMMIT="$GIT_COMMIT" \
YTA_BUILD_TIME="$BUILD_TIME" \
PYTHONPATH="$REPO_ROOT/backend${PYTHONPATH:+:$PYTHONPATH}" \
"$PYTHON" -m PyInstaller "$SPEC_FILE" \
    --distpath "$DIST_ROOT" \
    --workpath "$WORK_ROOT" \
    --noconfirm \
    || fail "PyInstaller freeze failed — see the output above"

EXE_NAME="$BUNDLE_NAME"
[ -f "$BUNDLE_DIR/$EXE_NAME" ] || EXE_NAME="$BUNDLE_NAME.exe"
[ -f "$BUNDLE_DIR/$EXE_NAME" ] || fail "freeze reported success but no executable at $BUNDLE_DIR/$BUNDLE_NAME[.exe]"
[ -x "$BUNDLE_DIR/$EXE_NAME" ] || chmod +x "$BUNDLE_DIR/$EXE_NAME" || true
info "Bundle executable: $BUNDLE_DIR/$EXE_NAME"

# ---------------------------------------------------------------------------
# 4b. Completeness gate: modules the app loads BY STRING at runtime
#     (invisible to static analysis) must be inside the frozen PYZ.
#     Their absence is silent at build time and fatal at runtime —
#     fail loudly here instead. (See the PYTHONPATH note in step 3.)
# ---------------------------------------------------------------------------
PYZ_TOC="$WORK_ROOT/$BUNDLE_NAME/PYZ-00.toc"
[ -f "$PYZ_TOC" ] || fail "completeness gate: $PYZ_TOC not found — cannot verify the frozen module set"
MISSING_MODULES=""
for mod in app.main app.routers.health app.routers.system app.routers.ws; do
    grep -q "'$mod'" "$PYZ_TOC" || MISSING_MODULES="${MISSING_MODULES} $mod"
done
[ -z "$MISSING_MODULES" ] || fail "completeness gate: frozen bundle is missing runtime-loaded module(s):$MISSING_MODULES — the spec's collect_submodules(\"app\") collected nothing (backend/ must be importable at spec-exec time; these scripts set PYTHONPATH for exactly this). Do NOT ship this bundle."
info "Completeness gate passed: runtime-loaded modules present in the frozen PYZ"

# ---------------------------------------------------------------------------
# 5. Hazard-H1 copy step (spec header): prompts/ BESIDE the executable.
# ---------------------------------------------------------------------------
[ -d "$REPO_ROOT/prompts" ] || fail "repo-root prompts/ directory not found — the frozen sidecar cannot assemble agent prompts without it"
rm -rf "$BUNDLE_DIR/prompts"
cp -r "$REPO_ROOT/prompts" "$BUNDLE_DIR/prompts"
info "Staged prompts/ beside the executable (hazard H1; mirror also in _internal/prompts via the spec)"

# ---------------------------------------------------------------------------
# 6. Version stamping inside the bundle
# ---------------------------------------------------------------------------
printf '%s\n' "$STAMP_VERSION" > "$BUNDLE_DIR/VERSION"
"$PYTHON" - "$BUNDLE_DIR/build-info.json" "$STAMP_VERSION" "$RECORD_VERSION" "$GIT_COMMIT" "$BUILD_TIME" "$PLATFORM" "$PYINSTALLER_VERSION" "$EXE_NAME" <<'PYEOF'
import json, sys
path, stamp, record, commit, built_at, platform_name, pyinstaller, exe = sys.argv[1:9]
info = {
    "product": "youtube-agent-sidecar",
    "bundle": "yta_sidecar",
    "version": stamp,
    "version_of_record": record,
    "git_commit": commit,
    "build_time_utc": built_at,
    "platform": platform_name,
    "pyinstaller": pyinstaller,
    "spec": "packaging/yta_sidecar.spec",
    "executable": exe,
    "layout": "pyinstaller --onedir",
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(info, fh, indent=2)
    fh.write("\n")
PYEOF
info "Stamped version $STAMP_VERSION into $BUNDLE_DIR/VERSION and build-info.json"

# ---------------------------------------------------------------------------
# 7. Stage for the Tauri shell (externalBin layout under src-tauri/binaries/)
# ---------------------------------------------------------------------------
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)            TRIPLE="x86_64-unknown-linux-gnu" ;;
    Linux-aarch64|Linux-arm64) TRIPLE="aarch64-unknown-linux-gnu" ;;
    Darwin-x86_64)           TRIPLE="x86_64-apple-darwin" ;;
    Darwin-arm64)            TRIPLE="aarch64-apple-darwin" ;;
    *)                       TRIPLE="" ;;
esac
if [ -n "$TRIPLE" ]; then
    STAGED_TREE="$STAGE_DIR/agent-backend"
    STAGED_EXE="$STAGE_DIR/agent-backend-$TRIPLE"
    [ "$EXE_NAME" = "$BUNDLE_NAME.exe" ] && STAGED_EXE="$STAGED_EXE.exe"
    rm -rf "$STAGED_TREE" "$STAGED_EXE"
    mkdir -p "$STAGE_DIR"
    cp -r "$BUNDLE_DIR" "$STAGED_TREE"
    cp "$BUNDLE_DIR/$EXE_NAME" "$STAGED_EXE"
    [ -x "$STAGED_EXE" ] || chmod +x "$STAGED_EXE" || true
    info "Staged for Tauri: $STAGED_EXE (+ full tree at $STAGED_TREE/)"
else
    warn "unrecognised platform $(uname -s)-$(uname -m) — skipped Tauri staging (bundle itself is complete)"
    STAGED_EXE=""
fi

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------
TOTAL_BYTES="$(du -sb "$BUNDLE_DIR" | cut -f1)"
TOTAL_HUMAN="$(du -sh "$BUNDLE_DIR" | cut -f1)"
FILE_COUNT="$(find "$BUNDLE_DIR" -type f | wc -l | tr -d ' ')"

printf '\n'
info "SIDECAR FREEZE COMPLETE"
printf '  Version (stamped) : %s\n' "$STAMP_VERSION"
printf '  Bundle path       : %s\n' "$BUNDLE_DIR"
printf '  Executable        : %s\n' "$BUNDLE_DIR/$EXE_NAME"
printf '  Bundle size       : %s bytes (%s on disk, %s files)\n' "$TOTAL_BYTES" "$TOTAL_HUMAN" "$FILE_COUNT"
if [ -n "${STAGED_EXE:-}" ]; then
    printf '  Tauri staging     : %s\n' "$STAGED_EXE"
fi
