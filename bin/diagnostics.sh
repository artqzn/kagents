#!/usr/bin/env bash
# bin/diagnostics.sh — Produce a diagnostic bundle for support.
#
# Usage: ./bin/diagnostics.sh
set -euo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
load_config

BUNDLE="/tmp/kag-diagnostics-$(date +%Y%m%d-%H%M%S).tar.gz"
DIAG_DIR=$(mktemp -d)

mkdir -p "$DIAG_DIR"
{
  echo "=== kernel-agents diagnostics ==="
  echo "Generated: $(date -u +%FT%TZ)"
  echo
  "${KAG_REPO_ROOT}/bin/status.sh" 2>&1 || true
  echo
  echo "=== Recent deploy log ==="
  ls -t "${KAG_REPO_ROOT}/logs/" 2>/dev/null | head -1 | while read -r f; do
    cat "${KAG_REPO_ROOT}/logs/$f"
  done
  echo
  echo "=== Docker info ==="
  docker info 2>&1 || true
  echo
  echo "=== Docker system df ==="
  docker system df 2>&1 || true
  echo
  echo "=== Docker images ==="
  docker images 2>&1 || true
  echo
  echo "=== Docker volumes ==="
  docker volume ls 2>&1 || true
} > "$DIAG_DIR/status.txt" 2>&1

cp "$KAG_WORKSPACE/state/orchestrator.db" "$DIAG_DIR/" 2>/dev/null || true
cp "$KAG_WORKSPACE/state/llm_costs.jsonl" "$DIAG_DIR/" 2>/dev/null || true
cp "$KAG_REPO_ROOT/VERSION" "$DIAG_DIR/" 2>/dev/null || true

# Include config (redact API keys)
{
  echo "=== default.env ==="
  cat "$KAG_REPO_ROOT/config/default.env"
  echo
  echo "=== ~/.kag.env (redacted) ==="
  if [[ -f ~/.kag.env ]]; then
    sed -E 's/(API_KEY=).*/\1<REDACTED>/' ~/.kag.env
  else
    echo "(not present)"
  fi
} > "$DIAG_DIR/config.txt" 2>&1

tar czf "$BUNDLE" -C "$DIAG_DIR" .
rm -rf "$DIAG_DIR"

echo "Diagnostic bundle: $BUNDLE"
echo "Email this to maintainers when reporting issues."
