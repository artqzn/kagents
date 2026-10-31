#!/usr/bin/env bash
# bin/resume-task.sh — Resume a failed/interrupted task from last good state.
#
# Usage: ./bin/resume-task.sh <task-id>
#
# The --resume flag and task ID are passed via environment variables to avoid
# docker compose run treating them as command overrides.
set -euo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
load_config

TASK_ID="${1:-}"
[[ -n "$TASK_ID" ]] || die "Usage: resume-task.sh <task-id>"

log "Resuming task: $TASK_ID"

# Pass resume flag and task ID via environment variables.
# The orchestrator reads KAG_RESUME=1 and KAG_RESUME_TASK_ID=<id>.
KAG_RESUME=1 \
KAG_RESUME_TASK_ID="$TASK_ID" \
compose_cmd run --rm \
  orchestrator
