#!/usr/bin/env bash
# bin/cleanup.sh — Remove kernel-agents installation.
#
# Usage:
#   ./bin/cleanup.sh [--all] [--yes]
#
# Options:
#   --all   Also remove the workspace (incl. linux.git clone)
#   --yes   Skip confirmation prompts
set -euo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
load_config

REMOVE_ALL=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --all)  REMOVE_ALL=1 ;;
    --yes)  ASSUME_YES=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "Unknown arg: $arg (try --help)" ;;
  esac
done

confirm() {
  local prompt="$1"
  if [[ "$ASSUME_YES" -eq 1 ]]; then return 0; fi
  read -r -p "? $prompt [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

# ─── Phase 1: Stop running containers ───────────────────────────────
phase "Stopping running containers"

RUNNING=$(docker ps --filter "label=${KAG_IMAGE_LABEL}" -q 2>/dev/null || true)
if [[ -n "$RUNNING" ]]; then
  echo "$RUNNING" | xargs -r docker stop
  ok "Stopped running containers"
else
  log "No running containers"
fi

if [[ -f "$KAG_DOCKER_COMPOSE" ]]; then
  docker compose -f "$KAG_DOCKER_COMPOSE" down --remove-orphans 2>/dev/null || true
  ok "Compose services stopped"
fi

# ─── Phase 2: Remove containers ─────────────────────────────────────
phase "Removing stopped containers"

STOPPED=$(docker ps -a --filter "label=${KAG_IMAGE_LABEL}" -q 2>/dev/null || true)
if [[ -n "$STOPPED" ]]; then
  echo "$STOPPED" | xargs -r docker rm -f
  ok "Removed containers"
else
  log "No stopped containers"
fi

# ─── Phase 3: Remove images ─────────────────────────────────────────
phase "Removing Docker images"

for img in kag-base kag-planner kag-implementer kag-reviewer kag-tester kag-archivist; do
  if docker image inspect "$img" >/dev/null 2>&1; then
    docker rmi -f "$img" >/dev/null 2>&1 && log "Removed image $img"
  fi
done
ok "Images removed"

# ─── Phase 4: Remove volumes ────────────────────────────────────────
phase "Removing Docker volumes"

for vol in "$KAG_VOLUME_CCACHE" "$KAG_VOLUME_BUILD"; do
  if docker volume inspect "$vol" >/dev/null 2>&1; then
    if confirm "Remove volume $vol? (contains build cache, recreating takes ~30 min)"; then
      docker volume rm "$vol" >/dev/null
      log "Removed volume $vol"
    else
      log "Keeping volume $vol"
    fi
  fi
done

# ─── Phase 5: Optionally remove workspace ───────────────────────────
if [[ "$REMOVE_ALL" -eq 1 ]]; then
  phase "Removing workspace at ${KAG_WORKSPACE}"
  if confirm "Delete entire workspace? This removes linux.git, all branches, artifacts, state DB"; then
    rm -rf "$KAG_WORKSPACE"
    ok "Workspace removed"
  else
    log "Keeping workspace"
  fi
else
  log "Workspace preserved at ${KAG_WORKSPACE}"
  log "Run with --all to also remove the workspace (incl. linux.git clone)"
fi

# ─── Phase 6: Optionally remove user config ─────────────────────────
if [[ -f "${HOME}/.kag.env" ]]; then
  if confirm "Remove ~/.kag.env?"; then
    rm -f "${HOME}/.kag.env"
    ok "Removed ~/.kag.env"
  fi
fi

# ─── Done ───────────────────────────────────────────────────────────
cat <<EOF

${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
${GREEN}✓ Cleanup complete${NC}

To reinstall: ./bin/deploy.sh
${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
EOF
