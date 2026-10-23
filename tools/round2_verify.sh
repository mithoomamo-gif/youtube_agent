#!/usr/bin/env bash
# =============================================================================
# round2_verify.sh — Round 2 integration verification runner
#
# YouTube Agent — runs every Round 2 acceptance / regression check from the
# repo root and prints a single summary block. It boots nothing itself (no
# sidecar, no dev server); it only runs test suites, frontend checks, and
# media probes.
#
# Steps:
#   1. Round 2 acceptance suite   backend/.venv/bin/python -m pytest tests/integration -q
#   2. Backend suite              backend/.venv/bin/python -m pytest backend/tests -q
#   3. Root suite                 backend/.venv/bin/python -m pytest tests -q
#   4. Frontend typecheck         npm run typecheck
#   5. Frontend build             npm run build
#   6. Media probe                ffprobe on every MP4 in outputs/round2/
#                                 (directory absent -> SKIPPED, not a failure)
#
# Robustness contract:
#   * Never dies on the first failure — every step runs, exit codes and
#     pass/fail counts are captured per step and summarised at the end.
#   * Exit code is 0 only if every non-skipped step passed; otherwise it is
#     the number of failed steps (capped at 125) so CI can distinguish
#     "how many things broke" at a glance while still being non-zero.
#   * If /tmp is constrained (nearly full / not writable), TMPDIR is
#     redirected to a workspace-local directory (<repo>/.tmp/round2-verify/)
#     so pytest / node / ffprobe temp usage cannot fail spuriously.
#
# Usage:
#   ./tools/round2_verify.sh              # run everything (no flags needed)
#
# Per-step logs are always kept under the work dir (path printed at the end)
# so a failing step can be diagnosed after the run.
#
# Logs: one file per step under "$WORK_TMP/logs/", plus the full console
# output on stdout. Bugs found by this run belong in docs/ROUND2_BUGS.md.
# =============================================================================

set +e
set -u

# ---------------------------------------------------------------------------
# Repo root: resolve from this script's location (tools/ is one level down),
# so the script works from any CWD. Fall back to the current directory.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
if [ -n "${SCRIPT_DIR:-}" ] && [ -f "$SCRIPT_DIR/../package.json" ]; then
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  REPO_ROOT="$(pwd)"
fi
cd "$REPO_ROOT" || { echo "ERROR: cannot cd to repo root ($REPO_ROOT)"; exit 2; }

echo "======================================================================"
echo " Round 2 verification — YouTube Agent"
echo " Repo:  $REPO_ROOT"
echo " Date:  $(date '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || date)"
echo "======================================================================"

# ---------------------------------------------------------------------------
# TMPDIR: /tmp on the dev VM is a small tmpfs that can sit at 100%.
# Detect a constrained /tmp and fall back to a workspace-local dir.
# ---------------------------------------------------------------------------
WORK_TMP="$REPO_ROOT/.tmp/round2-verify"
NEED_LOCAL_TMP=0
if [ ! -w /tmp ]; then
  NEED_LOCAL_TMP=1
else
  # Percent-used of the filesystem backing /tmp (POSIX df -P, field 5).
  TMP_USE_PCT="$(df -P /tmp 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
  case "$TMP_USE_PCT" in
    ''|*[!0-9]*) : ;;  # could not parse — leave TMPDIR alone
    *) if [ "$TMP_USE_PCT" -ge 90 ]; then NEED_LOCAL_TMP=1; fi ;;
  esac
fi
if [ "$NEED_LOCAL_TMP" -eq 1 ]; then
  mkdir -p "$WORK_TMP" 2>/dev/null
  export TMPDIR="$WORK_TMP"
  export TMP="$WORK_TMP"
  export TEMP="$WORK_TMP"
  echo "NOTE: /tmp is constrained — TMPDIR redirected to $WORK_TMP"
else
  # Still keep our own logs in a predictable workspace-local place.
  mkdir -p "$WORK_TMP" 2>/dev/null
fi
LOG_DIR="$WORK_TMP/logs"
mkdir -p "$LOG_DIR" 2>/dev/null

# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------
PY="$REPO_ROOT/backend/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "WARNING: backend venv python not found at $PY"
  if command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
    echo "         Falling back to system python3: $PY"
  elif command -v python >/dev/null 2>&1; then
    PY="$(command -v python)"
    echo "         Falling back to system python: $PY"
  else
    echo "ERROR: no Python interpreter found — pytest steps will fail."
    PY="python3"
  fi
fi
echo "Python:  $PY ($($PY --version 2>&1))"
if command -v npm >/dev/null 2>&1; then
  echo "npm:     $(npm --version 2>&1) / node $(node --version 2>&1)"
else
  echo "npm:     NOT FOUND — frontend steps will fail."
fi
if command -v ffprobe >/dev/null 2>&1; then
  echo "ffprobe: $(ffprobe -version 2>&1 | head -n 1)"
else
  echo "ffprobe: NOT FOUND — media probe step will be skipped."
fi
echo "Logs:    $LOG_DIR"
echo

# ---------------------------------------------------------------------------
# Step bookkeeping
# ---------------------------------------------------------------------------
STEP_NAMES=()
STEP_EXITS=()
STEP_RESULTS=()
STEP_DETAILS=()
FAILED_STEPS=0

# record_step <name> <exit_code> <result:PASS|FAIL|SKIPPED> <detail>
record_step() {
  STEP_NAMES+=("$1")
  STEP_EXITS+=("$2")
  STEP_RESULTS+=("$3")
  STEP_DETAILS+=("$4")
  if [ "$3" = "FAIL" ]; then
    FAILED_STEPS=$((FAILED_STEPS + 1))
  fi
}

# Extract "N passed / N failed / N skipped / N error(s)" counts from the
# last summary line of a pytest log. Prints a compact detail string.
pytest_counts() {
  local log_file="$1"
  local summary passed failed skipped errors deselected
  summary="$(grep -E '[0-9]+ (passed|failed|skipped|error)' "$log_file" 2>/dev/null | tail -n 1)"
  if [ -z "$summary" ]; then
    # No summary line: collection error, missing dir, or no tests ran.
    if grep -q "ERROR" "$log_file" 2>/dev/null; then
      echo "collection/runtime ERROR (see log)"
    elif grep -q "no tests ran" "$log_file" 2>/dev/null; then
      echo "no tests ran"
    else
      echo "no pytest summary found (see log)"
    fi
    return
  fi
  passed="$(printf '%s' "$summary" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | head -n 1)"
  failed="$(printf '%s' "$summary" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+' | head -n 1)"
  skipped="$(printf '%s' "$summary" | grep -oE '[0-9]+ skipped' | grep -oE '[0-9]+' | head -n 1)"
  errors="$(printf '%s' "$summary" | grep -oE '[0-9]+ errors?' | grep -oE '[0-9]+' | head -n 1)"
  deselected="$(printf '%s' "$summary" | grep -oE '[0-9]+ deselected' | grep -oE '[0-9]+' | head -n 1)"
  local out=""
  [ -n "$passed" ]     && out="$out ${passed} passed,"
  [ -n "$failed" ]     && out="$out ${failed} failed,"
  [ -n "$errors" ]     && out="$out ${errors} errors,"
  [ -n "$skipped" ]    && out="$out ${skipped} skipped,"
  [ -n "$deselected" ] && out="$out ${deselected} deselected,"
  out="${out%,}"
  printf '%s' "${out# }"
}

# run_pytest_step <name> <log_slug> <args...>
run_pytest_step() {
  local name="$1"; shift
  local slug="$1"; shift
  local log_file="$LOG_DIR/$slug.log"
  local start_s end_s ec detail result
  start_s="$(date +%s 2>/dev/null || echo 0)"
  echo "----------------------------------------------------------------------"
  echo "STEP: $name"
  echo "  $ $PY -m pytest $*"
  echo "----------------------------------------------------------------------"
  "$PY" -m pytest "$@" >"$log_file" 2>&1
  ec=$?
  cat "$log_file"
  end_s="$(date +%s 2>/dev/null || echo 0)"
  detail="$(pytest_counts "$log_file")"
  if [ "$start_s" != "0" ] && [ "$end_s" != "0" ]; then
    detail="$detail [$((end_s - start_s))s]"
  fi
  if [ "$ec" -eq 0 ]; then result="PASS"; else result="FAIL"; fi
  echo ">> $name: exit $ec — $detail"
  echo
  record_step "$name" "$ec" "$result" "$detail"
}

# run_cmd_step <name> <log_slug> <command...>
run_cmd_step() {
  local name="$1"; shift
  local slug="$1"; shift
  local log_file="$LOG_DIR/$slug.log"
  local start_s end_s ec result
  start_s="$(date +%s 2>/dev/null || echo 0)"
  echo "----------------------------------------------------------------------"
  echo "STEP: $name"
  echo "  $ $*"
  echo "----------------------------------------------------------------------"
  "$@" >"$log_file" 2>&1
  ec=$?
  cat "$log_file"
  end_s="$(date +%s 2>/dev/null || echo 0)"
  if [ "$ec" -eq 0 ]; then result="PASS"; else result="FAIL"; fi
  local detail="exit $ec"
  if [ "$start_s" != "0" ] && [ "$end_s" != "0" ]; then
    detail="$detail [$((end_s - start_s))s]"
  fi
  echo ">> $name: $result ($detail)"
  echo
  record_step "$name" "$ec" "$result" "$detail"
}

# ---------------------------------------------------------------------------
# Steps 1–3: pytest suites
# ---------------------------------------------------------------------------
# (a) Round 2 acceptance suite. Other Round 2 workers own the tests in
#     tests/integration/; if the directory has not landed yet pytest exits
#     non-zero / collects nothing and this step reports FAIL with the log —
#     that is a missing-deliverable signal for docs/ROUND2_BUGS.md, not a
#     script bug.
run_pytest_step "R2 acceptance (tests/integration)" "pytest-integration" tests/integration -q

# (b) Backend suite.
run_pytest_step "Backend suite (backend/tests)" "pytest-backend" backend/tests -q

# (c) Root suite (unit + contract + fault corpus + golden + integration,
#     per tests/pytest.ini; live tests excluded by default).
run_pytest_step "Root suite (tests)" "pytest-root" tests -q

# ---------------------------------------------------------------------------
# Steps 4–5: frontend
# ---------------------------------------------------------------------------
if [ -f "$REPO_ROOT/package.json" ]; then
  run_cmd_step "Frontend typecheck (npm run typecheck)" "npm-typecheck" npm run typecheck
  run_cmd_step "Frontend build (npm run build)" "npm-build" npm run build
else
  echo "STEP: Frontend — package.json not found, skipping both npm steps."
  record_step "Frontend typecheck (npm run typecheck)" "-" "SKIPPED" "package.json absent"
  record_step "Frontend build (npm run build)" "-" "SKIPPED" "package.json absent"
fi

# ---------------------------------------------------------------------------
# Step 6: ffprobe check on Round 2 outputs
# ---------------------------------------------------------------------------
OUTPUTS_DIR="$REPO_ROOT/outputs/round2"
FFPROBE_NAME="Media probe (outputs/round2/*.mp4)"
echo "----------------------------------------------------------------------"
echo "STEP: $FFPROBE_NAME"
echo "----------------------------------------------------------------------"
if [ ! -d "$OUTPUTS_DIR" ]; then
  echo "Directory $OUTPUTS_DIR does not exist yet — no Round 2 renders to probe."
  echo ">> $FFPROBE_NAME: SKIPPED (directory absent — expected until the"
  echo "   first end-to-end Round 2 render lands)"
  echo
  record_step "$FFPROBE_NAME" "-" "SKIPPED" "outputs/round2/ absent"
elif ! command -v ffprobe >/dev/null 2>&1; then
  echo "ffprobe not found on PATH — cannot probe media."
  echo ">> $FFPROBE_NAME: SKIPPED (ffprobe absent)"
  echo
  record_step "$FFPROBE_NAME" "-" "SKIPPED" "ffprobe absent"
else
  MP4_LIST="$LOG_DIR/mp4-list.txt"
  find "$OUTPUTS_DIR" -type f -iname '*.mp4' 2>/dev/null | sort >"$MP4_LIST"
  MP4_COUNT="$(wc -l <"$MP4_LIST" | tr -d ' ')"
  if [ "$MP4_COUNT" -eq 0 ]; then
    echo "No MP4 files found in $OUTPUTS_DIR."
    echo ">> $FFPROBE_NAME: SKIPPED (no MP4s yet)"
    echo
    record_step "$FFPROBE_NAME" "-" "SKIPPED" "no MP4s in outputs/round2/"
  else
    echo "Found $MP4_COUNT MP4 file(s):"
    PROBE_OK=0
    PROBE_BAD=0
    while IFS= read -r mp4; do
      [ -n "$mp4" ] || continue
      echo
      echo "--- $mp4"
      PROBE_OUT="$(ffprobe -v error \
        -show_entries format=duration,size,bit_rate \
        -show_entries stream=index,codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate \
        -of default=noprint_wrappers=1 "$mp4" 2>&1)"
      PROBE_EC=$?
      if [ "$PROBE_EC" -eq 0 ]; then
        printf '%s\n' "$PROBE_OUT"
        DUR="$(ffprobe -v error -show_entries format=duration \
          -of default=noprint_wrappers=1:nokey=1 "$mp4" 2>/dev/null)"
        echo "=> OK (duration: ${DUR:-unknown}s)"
        PROBE_OK=$((PROBE_OK + 1))
      else
        printf '%s\n' "$PROBE_OUT"
        echo "=> PROBE FAILED (ffprobe exit $PROBE_EC)"
        PROBE_BAD=$((PROBE_BAD + 1))
      fi
    done <"$MP4_LIST"
    echo
    if [ "$PROBE_BAD" -eq 0 ]; then
      echo ">> $FFPROBE_NAME: PASS ($PROBE_OK/$MP4_COUNT probed OK)"
      record_step "$FFPROBE_NAME" "0" "PASS" "$PROBE_OK/$MP4_COUNT MP4s probed OK"
    else
      echo ">> $FFPROBE_NAME: FAIL ($PROBE_BAD of $MP4_COUNT MP4s failed to probe)"
      record_step "$FFPROBE_NAME" "1" "FAIL" "$PROBE_BAD/$MP4_COUNT MP4s failed probe"
    fi
    echo
  fi
fi

# ---------------------------------------------------------------------------
# Summary block
# ---------------------------------------------------------------------------
echo "======================================================================"
echo " ROUND 2 VERIFICATION SUMMARY"
echo "======================================================================"
printf ' %-46s %-6s %-8s %s\n' "STEP" "EXIT" "RESULT" "DETAIL"
printf ' %-46s %-6s %-8s %s\n' "----------------------------------------------" "----" "------" "------"
i=0
while [ "$i" -lt "${#STEP_NAMES[@]}" ]; do
  printf ' %-46s %-6s %-8s %s\n' \
    "${STEP_NAMES[$i]}" "${STEP_EXITS[$i]}" "${STEP_RESULTS[$i]}" "${STEP_DETAILS[$i]}"
  i=$((i + 1))
done
echo "----------------------------------------------------------------------"
TOTAL_STEPS="${#STEP_NAMES[@]}"
PASSED_STEPS=0
SKIPPED_STEPS=0
i=0
while [ "$i" -lt "$TOTAL_STEPS" ]; do
  case "${STEP_RESULTS[$i]}" in
    PASS)    PASSED_STEPS=$((PASSED_STEPS + 1)) ;;
    SKIPPED) SKIPPED_STEPS=$((SKIPPED_STEPS + 1)) ;;
  esac
  i=$((i + 1))
done
echo " Steps: $TOTAL_STEPS total — $PASSED_STEPS passed, $FAILED_STEPS failed, $SKIPPED_STEPS skipped"
echo " Logs:  $LOG_DIR"
echo " Bugs:  log every failure in docs/ROUND2_BUGS.md"
if [ "$FAILED_STEPS" -eq 0 ]; then
  echo " VERDICT: PASS"
  echo "======================================================================"
  exit 0
else
  echo " VERDICT: FAIL ($FAILED_STEPS step(s) failed)"
  echo "======================================================================"
  # Non-zero, and equal to the failed-step count (capped for portability).
  if [ "$FAILED_STEPS" -gt 125 ]; then exit 125; fi
  exit "$FAILED_STEPS"
fi
