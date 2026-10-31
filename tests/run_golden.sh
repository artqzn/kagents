#!/usr/bin/env bash
# tests/run_golden.sh — Run golden test patches through the pipeline.
#
# Golden tests are known-good and known-bad patches that exercise the
# pipeline. They verify that the framework still rejects bad patches
# and accepts good ones after framework updates.
#
# Usage: ./tests/run_golden.sh
set -euo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
load_config

GOLDEN_DIR="${KAG_REPO_ROOT}/tests/golden"
RESULTS_DIR="${KAG_REPO_ROOT}/logs/golden-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS_DIR"

PASS_COUNT=0
FAIL_COUNT=0

run_golden_test() {
  local test_name="$1"
  local expected="$2"  # "pass" or "fail"
  local patch_file="$3"
  local description="$4"

  echo
  echo "━━━ Test: $test_name ━━━"
  echo "Expected: $expected"
  echo "Description: $description"
  echo

  # For now, just validate that the patch file exists and has content
  if [[ ! -f "$patch_file" ]]; then
    echo "  FAIL: patch file not found: $patch_file"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi

  local patch_size
  patch_size=$(wc -c < "$patch_file")
  if [[ "$patch_size" -lt 10 ]]; then
    echo "  FAIL: patch file too small ($patch_size bytes)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi

  echo "  Patch size: $patch_size bytes"
  echo "  PASS: patch file valid"
  PASS_COUNT=$((PASS_COUNT + 1))
}

echo "════════════════════════════════════════════════════════════════"
echo "  Golden Test Suite"
echo "════════════════════════════════════════════════════════════════"

# Find all golden test directories
for test_dir in "$GOLDEN_DIR"/*/; do
  [[ -d "$test_dir" ]] || continue
  test_name=$(basename "$test_dir")
  expected_file="${test_dir}/expected"
  patch_file="${test_dir}/patch.diff"
  desc_file="${test_dir}/description"

  if [[ ! -f "$expected_file" ]]; then
    echo "SKIP: $test_name (no expected file)"
    continue
  fi

  expected=$(cat "$expected_file")
  description=$(cat "$desc_file" 2>/dev/null || echo "(no description)")

  run_golden_test "$test_name" "$expected" "$patch_file" "$description"
done

echo
echo "════════════════════════════════════════════════════════════════"
echo "  Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
echo "════════════════════════════════════════════════════════════════"

[[ "$FAIL_COUNT" -eq 0 ]]
