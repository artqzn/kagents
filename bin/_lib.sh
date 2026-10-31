#!/usr/bin/env bash
# Shared helpers for kernel-agents CLI.
# Sourced by all bin/*.sh scripts.

KAG_REPO_ROOT="${KAG_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export KAG_REPO_ROOT  # Export so docker compose can substitute ${KAG_REPO_ROOT} in compose.yml

# Colors
RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[34m'; NC=$'\033[0m'

log()   { echo "[$(date +%H:%M:%S)] $*" >&2; }
ok()    { echo "${GREEN}✓${NC} $*" >&2; }
warn()  { echo "${YELLOW}!${NC} $*" >&2; }
die()   { echo "${RED}✗${NC} $*" >&2; exit 1; }
phase() { echo; echo "${BLUE}▶ $*${NC}" >&2; }

load_config() {
  source "${KAG_REPO_ROOT}/config/default.env"
  if [[ -f "${HOME}/.kag.env" ]]; then
    set -a; source "${HOME}/.kag.env"; set +a
  fi
  : "${KAG_SUBSYSTEM:=mm}"
  # KAG_WORKSPACE_HOST is the host path to the workspace, used by agent containers.
  # Inside the orchestrator container, KAG_WORKSPACE=/workspace (the mount point),
  # but agent containers need the host path to mount correctly.
  export KAG_WORKSPACE_HOST="${KAG_WORKSPACE}"
  # Export all KAG_ variables so docker compose can substitute them
  export KAG_REPO_ROOT KAG_WORKSPACE KAG_WORKSPACE_HOST KAG_SUBSYSTEM \
         KAG_DOCKER_COMPOSE KAG_VOLUME_CCACHE KAG_VOLUME_BUILD KAG_IMAGE_LABEL
}

# ─── Platform detection ─────────────────────────────────────────────
detect_platform() {
  case "$(uname -s)" in
    Darwin) echo "macos" ;;
    Linux)  echo "linux" ;;
    *)      die "Unsupported OS: $(uname -s). Only macOS and Linux are supported." ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    arm64|aarch64) echo "arm64" ;;
    x86_64)        echo "x86_64" ;;
    *)             die "Unsupported arch: $(uname -m). Only arm64 and x86_64 are supported." ;;
  esac
}

# ─── Docker check (single runtime) ──────────────────────────────────
check_docker() {
  command -v docker >/dev/null || return 1
  docker info >/dev/null 2>&1  || return 2
  return 0
}

# ─── KVM detection (Linux only) ─────────────────────────────────────
check_kvm_available() {
  [[ "$(detect_platform)" == "linux" ]] || return 1
  [[ -e /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]
}

# ─── VirtioFS detection (macOS only) ────────────────────────────────
# Docker 29.x with overlayfs storage driver doesn't report file-sharing
# in `docker info`. We check three sources in order:
#   1. `docker info` output (older Docker Desktop versions)
#   2. Docker Desktop settings.json (all Docker Desktop versions)
#   3. Performance-based check (fallback — measures actual bind-mount speed)
check_virtiofs() {
  [[ "$(detect_platform)" == "macos" ]] || return 1

  # Method 1: docker info reports it directly (Docker Desktop < 25.x)
  if docker info 2>/dev/null | grep -qi "virtiofs"; then
    return 0
  fi

  # Method 2: check Docker Desktop settings.json
  local settings_file
  for settings_file in \
    "$HOME/Library/Group Containers/group.com.docker/settings.json" \
    "$HOME/.docker/desktop/settings.json"; do
    if [[ -f "$settings_file" ]]; then
      # Check for filesharingImplementation field
      if grep -q '"filesharingImplementation"[[:space:]]*:[[:space:]]*"virtiofs"' \
         "$settings_file" 2>/dev/null; then
        return 0
      fi
      # If explicitly set to grpcfuse, that's a fail
      if grep -q '"filesharingImplementation"[[:space:]]*:[[:space:]]*"grpcfuse"' \
         "$settings_file" 2>/dev/null; then
        return 1
      fi
    fi
  done

  # Method 3: performance-based check (definitive but slower)
  # Time a simple bind-mount operation. VirtioFS: <1s. gRPC FUSE: 3+ s.
  # Only run if we have a workspace dir to test against.
  if [[ -d "$HOME" ]]; then
    local test_dir
    test_dir=$(mktemp -d "$HOME/.kag-virtiotest-XXXXXX" 2>/dev/null)
    if [[ -n "$test_dir" && -d "$test_dir" ]]; then
      # Create 100 small files to simulate kernel tree
      for i in $(seq 1 100); do
        touch "$test_dir/file_$i" 2>/dev/null || true
      done
      # Time how long it takes to list them from inside a container
      local elapsed
      elapsed=$(docker run --rm -v "$test_dir:/test:cached" \
        kag-base bash -c 'time ls /test > /dev/null 2>&1' 2>&1 \
        | grep real | awk '{print $2}' || echo "0s")
      rm -rf "$test_dir" 2>/dev/null || true
      # Parse elapsed time (format: 0m0.123s or 0m1.234s)
      local secs
      secs=$(echo "$elapsed" | sed 's/.*m\([0-9.]*\)s/\1/')
      if [[ -n "$secs" ]]; then
        # Use awk for floating-point comparison
        if awk "BEGIN {exit !($secs < 2.0)}"; then
          return 0  # fast — VirtioFS is working
        else
          return 1  # slow — likely gRPC FUSE
        fi
      fi
    fi
  fi

  # If all methods fail to determine, assume not available (conservative)
  return 1
}

# ─── Compose command with optional KVM override ─────────────────────
compose_cmd() {
  local args=("$@")
  local kvm_override="${KAG_REPO_ROOT}/docker/compose.kvm.yml"
  if [[ -f "$kvm_override" ]] && check_kvm_available; then
    docker compose -f "${KAG_DOCKER_COMPOSE}" -f "$kvm_override" "${args[@]}"
  else
    docker compose -f "${KAG_DOCKER_COMPOSE}" "${args[@]}"
  fi
}

# ─── Migration: legacy KMMA_ → KAG_ ─────────────────────────────────
migrate_legacy_config() {
  if [[ -f "${HOME}/.kmma.env" ]] && [[ ! -f "${HOME}/.kag.env" ]]; then
    warn "Found legacy ~/.kmma.env, migrating to ~/.kag.env"
    sed 's/KMMA_/KAG_/g' "${HOME}/.kmma.env" > "${HOME}/.kag.env"
    mv "${HOME}/.kmma.env" "${HOME}/.kmma.env.bak"
    ok "Migrated. Backup at ~/.kmma.env.bak"
  fi
}

# ─── Timeout command helper (macOS doesn't have `timeout` by default) ──
# Returns the timeout command prefix, or empty string if not available.
# Usage: $(get_timeout_cmd 30) some-command ...
# macOS users can install coreutils via Homebrew for gtimeout:
#   brew install coreutils
get_timeout_cmd() {
  local seconds="$1"
  if command -v timeout >/dev/null 2>&1; then
    echo "timeout ${seconds}"
  elif command -v gtimeout >/dev/null 2>&1; then
    echo "gtimeout ${seconds}"
  else
    # No timeout available — return empty (caller runs without timeout)
    echo ""
  fi
}

# ─── Clean up stale git worktrees and branches ─────────────────────
cleanup_worktrees() {
  local src="${KAG_WORKSPACE}/src"
  if [[ ! -d "$src/.git" ]]; then
    return
  fi
  log "Cleaning up stale git worktrees..."
  # Remove stale worktree registrations
  git -C "$src" worktree prune 2>/dev/null || true
  # List and remove worktrees under branches/ (except shared/main)
  if [[ -d "${KAG_WORKSPACE}/branches" ]]; then
    for wt_dir in "${KAG_WORKSPACE}/branches"/*/; do
      [[ -d "$wt_dir" ]] || continue
      wt_name=$(basename "$wt_dir")
      [[ "$wt_name" == "shared" ]] && continue
      for task_dir in "$wt_dir"*/; do
        [[ -d "$task_dir" ]] || continue
        git -C "$src" worktree remove --force "$task_dir" 2>/dev/null || true
        rm -rf "$task_dir" 2>/dev/null || true
      done
    done
  fi
  ok "Worktree cleanup complete"
}

# ─── Build Docker image if not present ──────────────────────────────
build_image() {
  local name="$1" dockerfile="$2" target="$3"
  if docker image inspect "$name" >/dev/null 2>&1; then
    log "Image $name exists, skipping (use --rebuild to force)"
    return
  fi
  log "Building $name..."
  if [[ -n "$target" ]]; then
    docker build --target "$target" -t "$name:latest" -f "$dockerfile" "${KAG_REPO_ROOT}" >&2 \
      || die "Failed to build $name"
  else
    docker build -t "$name:latest" -f "$dockerfile" "${KAG_REPO_ROOT}" >&2 \
      || die "Failed to build $name"
  fi
}
