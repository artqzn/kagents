#!/usr/bin/env bash
# bin/new-task.sh — Start a new kernel-agents task.
#
# Usage:
#   echo "<task description>" | ./bin/new-task.sh [--subsystem=mm] "<title>"
#
# The task description is read from stdin. The title is passed via
# environment variable (KAG_TASK_TITLE) to avoid docker compose run
# treating it as a command override.
set -euo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
load_config

SUBSYSTEM="${KAG_SUBSYSTEM:-mm}"
TITLE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --subsystem=*) SUBSYSTEM="${1#*=}"; shift ;;
    -h|--help)     sed -n '2,9p' "$0"; exit 0 ;;
    *)             TITLE="$1"; shift ;;
  esac
done

[[ -n "$TITLE" ]] || die "Usage: new-task.sh [--subsystem=mm] \"<title>\" <<EOF ... EOF"

DESC="$(cat)"

log "Starting task: $TITLE"
log "Subsystem: $SUBSYSTEM"

# Clean up any stale changes in /workspace/src from previous failed runs
if [[ -d "$KAG_WORKSPACE/src/.git" ]]; then
  git -C "$KAG_WORKSPACE/src" checkout -- . 2>/dev/null || true
  rm -f "$KAG_WORKSPACE/src/.git/index.lock" 2>/dev/null || true
fi

# Pass title via environment variable to avoid docker compose run
# treating it as a command override.
KAG_SUBSYSTEM="$SUBSYSTEM" \
KAG_TASK_TITLE="$TITLE" \
compose_cmd run --rm -T \
  orchestrator <<<"$DESC"
