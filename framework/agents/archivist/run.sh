#!/usr/bin/env bash
# Archivist — periodic log compression and artifact indexing.
# Runs as a cron-like background service.
set -euo pipefail

WORKSPACE="${KAG_WORKSPACE:-/workspace}"
ARTIFACTS_DIR="${WORKSPACE}/artifacts/test-results"
STATE_DB="${WORKSPACE}/state/orchestrator.db"

# Compress old test results (>7 days)
if [[ -d "$ARTIFACTS_DIR" ]]; then
  find "$ARTIFACTS_DIR" -maxdepth 1 -type d -mtime +7 -name "*" | \
    while IFS= read -r dir; do
    [[ "$dir" == "$ARTIFACTS_DIR" ]] && continue
    dir_name=$(basename "$dir")
    if [[ ! -f "${ARTIFACTS_DIR}/${dir_name}.tar.gz" ]]; then
      tar czf "${ARTIFACTS_DIR}/${dir_name}.tar.gz" \
        -C "$ARTIFACTS_DIR" "$dir_name" 2>/dev/null || true
      rm -rf "$dir"
      echo "[$(date)] Archived: $dir_name"
    fi
  done
fi

# Update SQLite index for queryability
if [[ -f "$STATE_DB" ]]; then
  find "$ARTIFACTS_DIR" -name "summary.json" 2>/dev/null | while IFS= read -r f; do
    run_id=$(basename "$(dirname "$f")")
    ok=$(python3 -c "import json; print(1 if json.load(open('$f')).get('ok') else 0)" 2>/dev/null || echo 0)
    sqlite3 "$STATE_DB" \
      "INSERT OR REPLACE INTO test_runs VALUES ('$run_id', '$(date -u +%FT%TZ)', $ok)" \
      2>/dev/null || true
  done
fi

# Clean up old LLM reasoning logs (>30 days)
REASONING_DIR="${WORKSPACE}/artifacts/llm_reasoning"
if [[ -d "$REASONING_DIR" ]]; then
  find "$REASONING_DIR" -name "*.md" -mtime +30 -delete 2>/dev/null || true
fi

echo "[$(date)] Archivist cycle complete"
