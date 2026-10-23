#!/usr/bin/env bash
# packaging/preflight.sh — YouTube Agent release preflight (POSIX/bash mirror
# of packaging/preflight.ps1; the two scripts implement the SAME checks).
#
# Usage:
#   ./packaging/preflight.sh [version]
#
#   [version]  Optional. The version being released (e.g. 0.1.0).
#              Defaults to the version in src-tauri/tauri.conf.json.
#
#   The repository root defaults to the parent directory of this script.
#   Set YTA_PREFLIGHT_ROOT to point the checks at a different tree
#   (used for fixture / positive-control runs).
#
# Self-contained: no network access, no secrets are read or printed.
# Every check prints PASS or FAIL with a one-line reason. The script
# exits 0 only if EVERY check passes; any FAIL exits non-zero, because
# a failed check is a genuine release blocker, not a warning.

set -u

ROOT="${YTA_PREFLIGHT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
TARGET_VERSION="${1:-}"

TAURI_CONF="$ROOT/src-tauri/tauri.conf.json"
CARGO_TOML="$ROOT/src-tauri/Cargo.toml"
PACKAGE_JSON="$ROOT/package.json"
BACKEND_INIT="$ROOT/backend/app/__init__.py"
BACKEND_PYPROJECT="$ROOT/backend/pyproject.toml"
MIGRATIONS_DIR="$ROOT/backend/migrations/versions"
CHANGELOG="$ROOT/CHANGELOG.md"

PASS_COUNT=0
FAIL_COUNT=0

report_pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    printf '[PASS] %s — %s\n' "$1" "$2"
}

report_fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf '[FAIL] %s — %s\n' "$1" "$2"
}

# ---------------------------------------------------------------------------
# Config extraction.
# tauri.conf.json is parsed with python3 when available, else jq. Both are
# local, offline tools; if neither exists the config checks fail loudly
# instead of guessing at JSON with regexes.
# ---------------------------------------------------------------------------
TAURI_VERSION=""
TAURI_RESOLVE_ERROR=""
ICON_LIST=""
TARGET_LIST=""
NSIS_INSTALL_MODE=""
UPDATER_PUBKEY="__MISSING__"
ENDPOINT_LIST=""
CONFIG_ERROR=""

extract_with_python() {
    python3 - "$TAURI_CONF" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        cfg = json.load(fh)
except Exception as exc:  # noqa: BLE001 - reason is printed for the user
    print("CONFIG_ERROR=could not parse src-tauri/tauri.conf.json: %s" % exc)
    sys.exit(0)

def emit(key, value):
    print("%s=%s" % (key, value))

emit("TAURI_VERSION", cfg.get("version", ""))
bundle = cfg.get("bundle", {}) or {}
for icon in bundle.get("icon", []) or []:
    emit("ICON", icon)
for target in bundle.get("targets", []) or []:
    emit("TARGET", target)
nsis = ((bundle.get("windows", {}) or {}).get("nsis", {}) or {})
emit("NSIS_INSTALL_MODE", nsis.get("installMode", ""))
updater = ((cfg.get("plugins", {}) or {}).get("updater", {}) or {})
if "pubkey" in updater:
    emit("UPDATER_PUBKEY", updater.get("pubkey") or "")
else:
    emit("UPDATER_PUBKEY", "__MISSING__")
for endpoint in updater.get("endpoints", []) or []:
    emit("ENDPOINT", endpoint)
PYEOF
}

extract_with_jq() {
    {
        printf 'TAURI_VERSION=%s\n' "$(jq -r '.version // ""' "$TAURI_CONF")"
        jq -r '(.bundle.icon // [])[] | "ICON=\(.)"' "$TAURI_CONF"
        jq -r '(.bundle.targets // [])[] | "TARGET=\(.)"' "$TAURI_CONF"
        printf 'NSIS_INSTALL_MODE=%s\n' "$(jq -r '.bundle.windows.nsis.installMode // ""' "$TAURI_CONF")"
        if jq -e '.plugins.updater | has("pubkey")' "$TAURI_CONF" >/dev/null 2>&1; then
            printf 'UPDATER_PUBKEY=%s\n' "$(jq -r '.plugins.updater.pubkey // ""' "$TAURI_CONF")"
        else
            printf 'UPDATER_PUBKEY=__MISSING__\n'
        fi
        jq -r '(.plugins.updater.endpoints // [])[] | "ENDPOINT=\(.)"' "$TAURI_CONF"
    }
}

if [ ! -f "$TAURI_CONF" ]; then
    CONFIG_ERROR="src-tauri/tauri.conf.json not found under $ROOT"
elif command -v python3 >/dev/null 2>&1; then
    EXTRACTED="$(extract_with_python)"
elif command -v jq >/dev/null 2>&1; then
    EXTRACTED="$(extract_with_jq)"
else
    CONFIG_ERROR="neither python3 nor jq is available to parse tauri.conf.json"
    EXTRACTED=""
fi

if [ -z "${CONFIG_ERROR:-}" ] && [ -n "${EXTRACTED:-}" ]; then
    while IFS= read -r line; do
        case "$line" in
            CONFIG_ERROR=*) CONFIG_ERROR="${line#CONFIG_ERROR=}" ;;
            TAURI_VERSION=*) TAURI_VERSION="${line#TAURI_VERSION=}" ;;
            ICON=*) ICON_LIST="${ICON_LIST}${line#ICON=}
" ;;
            TARGET=*) TARGET_LIST="${TARGET_LIST}${line#TARGET=}
" ;;
            NSIS_INSTALL_MODE=*) NSIS_INSTALL_MODE="${line#NSIS_INSTALL_MODE=}" ;;
            UPDATER_PUBKEY=*) UPDATER_PUBKEY="${line#UPDATER_PUBKEY=}" ;;
            ENDPOINT=*) ENDPOINT_LIST="${ENDPOINT_LIST}${line#ENDPOINT=}
" ;;
        esac
    done <<EOF
$EXTRACTED
EOF
fi

# ---------------------------------------------------------------------------
# Resolve the Tauri version indirection: tauri.conf.json's version may be
# a path reference (e.g. "../package.json", ending in .json) instead of a
# literal semver. When it is, read the referenced file (relative to
# src-tauri/) and use ITS version as the tauri version everywhere below
# (default target version + check a). A missing/unparseable referenced
# file fails check (a) with the resolve reason.
# ---------------------------------------------------------------------------
resolve_referenced_version() {
    # $1 = path to a JSON file; prints its top-level "version" or nothing.
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$1" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.load(fh).get("version", "") or "")
except Exception:
    pass
PYEOF
    elif command -v jq >/dev/null 2>&1; then
        jq -r '.version // ""' "$1" 2>/dev/null
    else
        sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -n 1
    fi
}

if [ -z "$CONFIG_ERROR" ]; then
    case "$TAURI_VERSION" in
        *.json)
            REF_PATH="$ROOT/src-tauri/$TAURI_VERSION"
            if [ ! -f "$REF_PATH" ]; then
                TAURI_RESOLVE_ERROR="tauri.conf.json version references '${TAURI_VERSION}' but the referenced file was not found: ${REF_PATH}"
            else
                RESOLVED_VERSION="$(resolve_referenced_version "$REF_PATH")"
                if [ -z "$RESOLVED_VERSION" ]; then
                    TAURI_RESOLVE_ERROR="tauri.conf.json version references '${TAURI_VERSION}' but no version could be parsed from ${REF_PATH}"
                else
                    TAURI_VERSION="$RESOLVED_VERSION"
                fi
            fi
            ;;
    esac
fi

if [ -z "$TARGET_VERSION" ]; then
    TARGET_VERSION="$TAURI_VERSION"
fi

# Small extraction helpers for the non-JSON version sources.
cargo_version() {
    [ -f "$CARGO_TOML" ] || return 0
    sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CARGO_TOML" | head -n 1
}

package_json_version() {
    [ -f "$PACKAGE_JSON" ] || return 0
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PACKAGE_JSON" | head -n 1
}

backend_init_version() {
    [ -f "$BACKEND_INIT" ] || return 0
    sed -n 's/^[[:space:]]*__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$BACKEND_INIT" | head -n 1
}

backend_pyproject_version() {
    [ -f "$BACKEND_PYPROJECT" ] || return 0
    sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$BACKEND_PYPROJECT" | head -n 1
}

printf 'YouTube Agent — release preflight\n'
printf 'Root: %s\n' "$ROOT"
printf 'Target version: %s\n' "${TARGET_VERSION:-<unknown>}"
printf '%s\n' '----------------------------------------------------------------'

# ---------------------------------------------------------------------------
# a. Version consistency:
#    tauri.conf.json (release-facing source of truth) == Cargo.toml
#    == package.json == backend version sources (app/__init__.py
#    __version__ and backend/pyproject.toml, both of which exist here).
# ---------------------------------------------------------------------------
check_versions() {
    local name='a. Version consistency'
    if [ -n "$CONFIG_ERROR" ]; then
        report_fail "$name" "$CONFIG_ERROR"
        return
    fi
    if [ -n "$TAURI_RESOLVE_ERROR" ]; then
        report_fail "$name" "$TAURI_RESOLVE_ERROR"
        return
    fi
    local cargo_v pkg_v be_init_v be_proj_v
    cargo_v="$(cargo_version)"
    pkg_v="$(package_json_version)"
    be_init_v="$(backend_init_version)"
    be_proj_v="$(backend_pyproject_version)"

    if [ -z "$TAURI_VERSION" ]; then
        report_fail "$name" 'tauri.conf.json has no version'
        return
    fi
    local problems=""
    [ "$cargo_v" = "$TAURI_VERSION" ] || problems="${problems} Cargo.toml='${cargo_v:-<missing>}'"
    [ "$pkg_v" = "$TAURI_VERSION" ] || problems="${problems} package.json='${pkg_v:-<missing>}'"
    if [ -f "$BACKEND_INIT" ]; then
        [ "$be_init_v" = "$TAURI_VERSION" ] || problems="${problems} backend/app/__init__.py __version__='${be_init_v:-<missing>}'"
    fi
    if [ -f "$BACKEND_PYPROJECT" ]; then
        [ "$be_proj_v" = "$TAURI_VERSION" ] || problems="${problems} backend/pyproject.toml='${be_proj_v:-<missing>}'"
    fi
    if [ -n "$problems" ]; then
        report_fail "$name" "tauri.conf.json='${TAURI_VERSION}' but mismatched:${problems}"
    else
        report_pass "$name" "tauri.conf.json, Cargo.toml, package.json and backend version sources all = ${TAURI_VERSION}"
    fi
}

# ---------------------------------------------------------------------------
# b. Icon set present: every file listed in bundle.icon exists (relative
#    to src-tauri/) and is non-empty.
# ---------------------------------------------------------------------------
check_icons() {
    local name='b. Icon set present'
    if [ -n "$CONFIG_ERROR" ]; then
        report_fail "$name" "$CONFIG_ERROR"
        return
    fi
    if [ -z "$ICON_LIST" ]; then
        report_fail "$name" 'tauri.conf.json bundle.icon is empty or missing'
        return
    fi
    local missing="" icon
    while IFS= read -r icon; do
        [ -n "$icon" ] || continue
        if [ ! -s "$ROOT/src-tauri/$icon" ]; then
            missing="${missing} ${icon}"
        fi
    done <<EOF
$ICON_LIST
EOF
    if [ -n "$missing" ]; then
        report_fail "$name" "missing or empty icon file(s) referenced by bundle.icon:${missing} (see src-tauri/icons/README.txt)"
    else
        report_pass "$name" 'every bundle.icon file exists and is non-empty'
    fi
}

# ---------------------------------------------------------------------------
# c. Updater: pubkey present and non-empty; every endpoint is https://
#    and not a placeholder host (example.invalid / example.com / .org /
#    .net, or localhost).
# ---------------------------------------------------------------------------
is_placeholder_host() {
    case "$1" in
        example.invalid|example.com|example.org|example.net|localhost|127.0.0.1) return 0 ;;
        *.example.invalid|*.example.com|*.example.org|*.example.net) return 0 ;;
        *) return 1 ;;
    esac
}

check_updater() {
    local name='c. Updater pubkey + endpoints'
    if [ -n "$CONFIG_ERROR" ]; then
        report_fail "$name" "$CONFIG_ERROR"
        return
    fi
    local problems=""
    if [ "$UPDATER_PUBKEY" = "__MISSING__" ]; then
        problems="${problems} plugins.updater.pubkey is missing;"
    elif [ -z "$UPDATER_PUBKEY" ]; then
        problems="${problems} plugins.updater.pubkey is empty (no signing key configured — updates cannot be verified);"
    fi
    if [ -z "$ENDPOINT_LIST" ]; then
        problems="${problems} plugins.updater.endpoints is empty;"
    else
        local endpoint host
        while IFS= read -r endpoint; do
            [ -n "$endpoint" ] || continue
            case "$endpoint" in
                https://*) ;;
                *) problems="${problems} endpoint '${endpoint}' is not https://;" ; continue ;;
            esac
            host="$(printf '%s' "$endpoint" | sed -e 's|^https://||' -e 's|[:/].*$||')"
            if is_placeholder_host "$host"; then
                problems="${problems} endpoint '${endpoint}' uses placeholder host '${host}';"
            fi
        done <<EOF
$ENDPOINT_LIST
EOF
    fi
    if [ -n "$problems" ]; then
        report_fail "$name" "${problems# }"
    else
        report_pass "$name" 'pubkey is non-empty and all updater endpoints are https:// on non-placeholder hosts'
    fi
}

# ---------------------------------------------------------------------------
# d. Migration chain single head: parse revision / down_revision from
#    every file in backend/migrations/versions. Exactly one head (a
#    revision no other revision points to), no duplicate revisions, no
#    dangling down_revision, exactly one root (down_revision = None).
# ---------------------------------------------------------------------------
check_migrations() {
    local name='d. Migration chain single head'
    if [ ! -d "$MIGRATIONS_DIR" ]; then
        report_fail "$name" "migrations directory not found: $MIGRATIONS_DIR"
        return
    fi
    local revisions="" downs="" file rev down
    for file in "$MIGRATIONS_DIR"/*.py; do
        [ -e "$file" ] || continue
        case "$(basename "$file")" in __init__.py) continue ;; esac
        rev="$(sed -n 's/^[[:space:]]*revision[[:space:]]*\(:[^=]*\)\?=[[:space:]]*"\([^"]*\)".*/\2/p' "$file" | head -n 1)"
        down="$(sed -n 's/^[[:space:]]*down_revision[[:space:]]*\(:[^=]*\)\?=[[:space:]]*\(None\|"\([^"]*\)"\).*/\3/p' "$file" | head -n 1)"
        if [ -z "$rev" ]; then
            report_fail "$name" "could not parse a revision in $(basename "$file")"
            return
        fi
        revisions="${revisions} ${rev}"
        # Empty down = root (down_revision = None) or a parse gap; the
        # dangling/root checks below distinguish them via the raw line.
        if grep -Eq '^[[:space:]]*down_revision[[:space:]]*(:[^=]*)?=[[:space:]]*None' "$file"; then
            downs="${downs} __ROOT__:${rev}"
        elif [ -n "$down" ]; then
            downs="${downs} ${down}"
        else
            report_fail "$name" "could not parse down_revision in $(basename "$file")"
            return
        fi
    done
    if [ -z "$revisions" ]; then
        report_fail "$name" 'no migration files found'
        return
    fi
    # Duplicate revisions?
    local dup
    dup="$(printf '%s\n' $revisions | sort | uniq -d | tr '\n' ' ')"
    if [ -n "$dup" ]; then
        report_fail "$name" "duplicate revision(s): ${dup}"
        return
    fi
    # Dangling down_revisions and root count.
    local roots=0 d r
    for d in $downs; do
        case "$d" in
            __ROOT__:*) roots=$((roots + 1)) ;;
            *)
                local found=0
                for r in $revisions; do [ "$r" = "$d" ] && found=1; done
                if [ "$found" -eq 0 ]; then
                    report_fail "$name" "down_revision '${d}' does not match any revision (broken chain)"
                    return
                fi
                ;;
        esac
    done
    if [ "$roots" -ne 1 ]; then
        report_fail "$name" "expected exactly 1 root migration (down_revision = None), found ${roots}"
        return
    fi
    # Heads = revisions that no down_revision points to.
    local heads="" head_count=0
    for r in $revisions; do
        local referenced=0
        for d in $downs; do [ "$d" = "$r" ] && referenced=1; done
        if [ "$referenced" -eq 0 ]; then
            heads="${heads} ${r}"
            head_count=$((head_count + 1))
        fi
    done
    if [ "$head_count" -ne 1 ]; then
        report_fail "$name" "expected exactly 1 migration head, found ${head_count}:${heads}"
    else
        report_pass "$name" "single migration head:${heads} (chain of $(printf '%s\n' $revisions | wc -l | tr -d ' ') revisions, 1 root, no duplicates or dangling links)"
    fi
}

# ---------------------------------------------------------------------------
# e. Changelog entry exists for the target version.
# ---------------------------------------------------------------------------
check_changelog() {
    local name='e. Changelog entry'
    if [ -z "$TARGET_VERSION" ]; then
        report_fail "$name" 'no target version (tauri.conf.json version missing and no version argument given)'
        return
    fi
    if [ ! -f "$CHANGELOG" ]; then
        report_fail "$name" 'CHANGELOG.md not found at repo root'
        return
    fi
    local escaped
    escaped="$(printf '%s' "$TARGET_VERSION" | sed 's/\./\\./g')"
    if grep -Eq "^##[[:space:]]+\[?${escaped}(\]|[^0-9.])" "$CHANGELOG" || grep -Eq "^##[[:space:]]+\[?${escaped}\]?[[:space:]]*\$" "$CHANGELOG"; then
        report_pass "$name" "CHANGELOG.md has a heading for version ${TARGET_VERSION}"
    else
        report_fail "$name" "CHANGELOG.md has no '## [${TARGET_VERSION}]' (or '## ${TARGET_VERSION}') heading"
    fi
}

# ---------------------------------------------------------------------------
# f. NSIS target + per-user install mode still configured (guard against
#    silent config drift in tauri.conf.json).
# ---------------------------------------------------------------------------
check_nsis() {
    local name='f. NSIS target + per-user install mode'
    if [ -n "$CONFIG_ERROR" ]; then
        report_fail "$name" "$CONFIG_ERROR"
        return
    fi
    local has_nsis=0 target
    while IFS= read -r target; do
        [ "$target" = "nsis" ] && has_nsis=1
    done <<EOF
$TARGET_LIST
EOF
    if [ "$has_nsis" -ne 1 ]; then
        report_fail "$name" "bundle.targets does not include 'nsis' (found:${TARGET_LIST:- <none>})"
    elif [ "$NSIS_INSTALL_MODE" != "currentUser" ]; then
        report_fail "$name" "bundle.windows.nsis.installMode is '${NSIS_INSTALL_MODE:-<missing>}', expected 'currentUser'"
    else
        report_pass "$name" "bundle.targets includes 'nsis' and bundle.windows.nsis.installMode = 'currentUser'"
    fi
}

check_versions
check_icons
check_updater
check_migrations
check_changelog
check_nsis

printf '%s\n' '----------------------------------------------------------------'
printf 'Summary: %d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
if [ "$FAIL_COUNT" -gt 0 ]; then
    printf 'Preflight FAILED — resolve the FAIL item(s) above before releasing.\n'
    exit 1
fi
printf 'Preflight PASSED — all checks green for version %s.\n' "$TARGET_VERSION"
exit 0
