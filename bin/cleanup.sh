#!/usr/bin/env bash
# bin/cleanup.sh — Remove kernel-agents installation.
#
# Usage:
#   ./bin/cleanup.sh [--all] [--yes]
#
# Options:
#   --all   Also remove the source tree (linux.git clone) and ccache
#   --yes   Skip confirmation prompts
#
# Default (without --all): removes everything EXCEPT the source tree
# (src/) and ccache volume. This lets you re-deploy quickly without
# re-cloning linux.git (5-10 min) or rebuilding ccache (30+ min).
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
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
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

# ─── Phase 4: Remove workspace data (KEEP src/ and ccache) ──────────
phase "Cleaning workspace data (preserving src/ and ccache)"

if [[ -d "$KAG_WORKSPACE" ]]; then
  # Remove everything except src/ and state DB
  # Keep: src/ (linux.git clone — 5-10 min to re-clone)
  # Remove: branches/, artifacts/, docs/, initramfs/, state/, .kbuild/
  for item in branches artifacts docs initramfs state .kbuild; do
    if [[ -e "$KAG_WORKSPACE/$item" ]]; then
      rm -rf "$KAG_WORKSPACE/$item"
      log "Removed $item/"
    fi
  done

  # Recreate empty dirs so deploy.sh doesn't complain
  mkdir -p "$KAG_WORKSPACE"/{branches/impl,artifacts,docs/rfcs,initramfs,state}
  ok "Workspace data cleaned (src/ preserved)"
else
  log "Workspace not found"
fi

# ─── Phase 5: Remove ccache volume (only with --all) ────────────────
if [[ "$REMOVE_ALL" -eq 1 ]]; then
  phase "Removing ccache volume (full cleanup)"

  if docker volume inspect "$KAG_VOLUME_CCACHE" >/dev/null 2>&1; then
    if confirm "Remove ccache volume? (rebuilding takes ~30 min)"; then
      docker volume rm "$KAG_VOLUME_CCACHE" >/dev/null
      log "Removed ccache volume"
    else
      log "Keeping ccache volume"
    fi
  fi

  # Also remove src/ with --all
  if [[ -d "$KAG_WORKSPACE/src" ]]; then
    if confirm "Remove source tree (src/)? (re-cloning takes 5-10 min)"; then
      rm -rf "$KAG_WORKSPACE/src"
      ok "Source tree removed"
    else
      log "Keeping source tree"
    fi
  fi
else
  log "ccache volume preserved (use --all to remove)"
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

Preserved:
  - src/ (linux.git clone)
  - ccache volume (build cache)

Removed:
  - branches/, artifacts/, docs/, initramfs/, state/
  - Docker images and containers

To re-deploy: ./bin/deploy.sh
  (src/ and ccache are preserved, so deploy is fast)
${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
EOF
