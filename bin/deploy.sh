#!/usr/bin/env bash
# bin/deploy.sh — Push-button installer for kernel-agents.
#
# Usage:
#   ./bin/deploy.sh [--subsystem=mm] [--skip-clone] [--skip-sanity]
#
# Phases:
#   0. Pre-flight checks (platform, Docker, KVM/VirtioFS, RAM, disk, API key)
#   1. Create workspace directory structure
#   2. Clone linux.git (depth=1) if not present
#   3. Create Docker named volumes (kag-ccache)
#   4. Build all 6 Docker images
#   5. Configure ccache (15GB, compression on)
#   6. Build initramfs
#   7. Sanity check: build defconfig kernel, boot under QEMU
#   8. Validate subsystem prompt resolution
set -euo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
migrate_legacy_config
load_config

LOG_FILE="${KAG_REPO_ROOT}/logs/deploy-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "${KAG_REPO_ROOT}/logs"

SKIP_CLONE=0
SKIP_SANITY=0
for arg in "$@"; do
  case "$arg" in
    --subsystem=*) KAG_SUBSYSTEM="${arg#*=}"; export KAG_SUBSYSTEM ;;
    --skip-clone)  SKIP_CLONE=1 ;;
    --skip-sanity) SKIP_SANITY=1 ;;
    --rebuild)     export KAG_REBUILD=1 ;;
    -h|--help)     sed -n '2,15p' "$0"; exit 0 ;;
    *) die "Unknown arg: $arg (try --help)" ;;
  esac
done

# ─── Phase 0: Pre-flight ────────────────────────────────────────────
phase "Phase 0: Pre-flight checks"

PLATFORM="$(detect_platform)"
ARCH="$(detect_arch)"
log "Detected: $PLATFORM / $ARCH"

case "$PLATFORM/$ARCH" in
  macos/arm64)  log "Platform: macOS / Apple Silicon — native ARM64 kernel compile" ;;
  linux/arm64)  log "Platform: Linux / arm64 — native ARM64 kernel compile" ;;
  linux/x86_64) log "Platform: Linux / x86_64 — will cross-compile ARM64 kernel" ;;
  macos/x86_64) die "Intel Mac not supported. Use Apple Silicon or Linux." ;;
esac

if ! check_docker; then
  cat <<EOF
Docker not found or not running. Install Docker:
  Linux:   https://docs.docker.com/engine/install/
           (Docker CE or Docker Engine)
  macOS:   Docker Desktop: https://docker.com/desktop
           Enable VirtioFS in Settings → General → "Choose file sharing
           implementation for your containers" → VirtioFS
Then start the Docker daemon and re-run this script.
EOF
  exit 1
fi
ok "Docker: $(docker --version)"

if [[ "$PLATFORM" == "macos" ]]; then
  if check_virtiofs; then
    ok "VirtioFS enabled — good bind-mount perf"
  else
    warn "VirtioFS not detected. Enable in Docker Desktop:"
    warn "  Settings → General → 'Choose file sharing' → VirtioFS"
    warn "  Without VirtioFS, kernel tree operations will be 5-10x slower."
    warn "  (If you're on Docker 29.x with overlayfs, VirtioFS may be working"
    warn "   even if not detected — deploy will continue.)"
  fi
elif [[ "$PLATFORM" == "linux" ]]; then
  ok "Docker running natively on Linux — bind-mount perf is native"
  if check_kvm_available; then
    ok "/dev/kvm accessible — Tester will use KVM acceleration (10-50x faster)"
  else
    warn "/dev/kvm not accessible — Tester will fall back to TCG (slower)"
    warn "  To enable: sudo usermod -aG kvm \$USER && log out/in"
  fi
fi

# Memory check
if [[ "$PLATFORM" == "linux" ]]; then
  MEM_GB=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}')
else
  MEM_GB=$(sysctl -n hw.memsize 2>/dev/null | awk '{print int($1/1073741824)}')
fi
[[ "${MEM_GB:-0}" -ge 16 ]] || warn "Found ${MEM_GB:-unknown}GB RAM. 16GB recommended."

# Disk space check
if [[ "$PLATFORM" == "linux" ]]; then
  DISK_FREE_GB=$(df -BG "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' | tr -d 'G')
else
  DISK_FREE_GB=$(df -g "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')
fi
[[ "${DISK_FREE_GB:-0}" -ge 50 ]] || warn "Only ${DISK_FREE_GB:-unknown}GB free. 50GB+ needed."

# API key check
HAS_KEY=0
for var in DEEPSEEK_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY ZAI_API_KEY; do
  if [[ -n "${!var:-}" ]]; then HAS_KEY=1; break; fi
done
if [[ "$HAS_KEY" -eq 0 ]]; then
  die "No LLM API key found. Add DEEPSEEK_API_KEY (or another provider) to ~/.kag.env"
fi
ok "LLM API key configured"

# Validate subsystem
if [[ ! -f "${KAG_REPO_ROOT}/subsystems/${KAG_SUBSYSTEM}/profile.py" ]]; then
  die "Unknown subsystem: $KAG_SUBSYSTEM. Available: $(ls "${KAG_REPO_ROOT}/subsystems/")"
fi

ok "Pre-flight checks passed (subsystem: $KAG_SUBSYSTEM)"

# ─── Phase 1: Workspace ─────────────────────────────────────────────
phase "Phase 1: Creating workspace at ${KAG_WORKSPACE}"

# Clean workspace layout:
#   src/                         — linux.git clone (stays clean, no build artifacts)
#   branches/impl/<task-id>/     — git worktrees for implementation
#   artifacts/<task-id>/         — per-task artifacts (build, test, review)
#     ├── build/                 — kernel Image, .config, System.map, build.log
#     ├── test/                  — boot.log, kunit.log, ltp.log
#     └── review/                — review comments, checkpatch output
#   docs/rfcs/                   — RFC documents
#   initramfs/                   — base initramfs for QEMU
#   state/                       — SQLite orchestrator.db
mkdir -p "${KAG_WORKSPACE}"/{branches/impl,artifacts,docs/rfcs,initramfs,state}

ok "Workspace ready"

# ─── Phase 2: Clone linux.git ───────────────────────────────────────
phase "Phase 2: Cloning linux.git (5-10 minutes)"

if [[ -d "${KAG_WORKSPACE}/src/.git" ]]; then
  ok "linux.git already present"
else
  git clone --depth=1 --branch "${KAG_KERNEL_BRANCH}" \
    "${KAG_KERNEL_REPO}" "${KAG_WORKSPACE}/src" 2>&1 | tee -a "$LOG_FILE"
  ok "linux.git cloned"
fi

# ─── Phase 3: Docker volumes ────────────────────────────────────────
phase "Phase 3: Creating Docker volumes"

ensure_volume() {
  if docker volume inspect "$1" >/dev/null 2>&1; then
    log "Volume $1 exists"
  else
    docker volume create "$1" >/dev/null
    ok "Created volume $1"
  fi
}
ensure_volume "$KAG_VOLUME_CCACHE"

# ─── Phase 4: Build images ──────────────────────────────────────────
phase "Phase 4: Building container images (10-15 minutes first time)"

cd "${KAG_REPO_ROOT}/docker"

if [[ -n "${KAG_REBUILD:-}" ]]; then
  for img in kag-base kag-planner kag-implementer kag-reviewer kag-tester kag-archivist; do
    docker rmi -f "$img" 2>/dev/null || true
  done
fi

build_image "kag-base" "base.Dockerfile" "orchestrator"
build_image "kag-planner" "base.Dockerfile" "planner"
build_image "kag-implementer" "base.Dockerfile" "implementer"
build_image "kag-reviewer" "base.Dockerfile" "reviewer"
build_image "kag-tester" "base.Dockerfile" "tester"
build_image "kag-archivist" "base.Dockerfile" "archivist"
ok "All images built"

# ─── Phase 5: ccache ────────────────────────────────────────────────
phase "Phase 5: Configuring ccache"

docker run --rm -v "${KAG_VOLUME_CCACHE}:/root/.ccache" \
  kag-base ccache -M 15G >/dev/null
docker run --rm -v "${KAG_VOLUME_CCACHE}:/root/.ccache" \
  kag-base ccache --set-config=compression=true >/dev/null
ok "ccache configured (15GB, compressed)"

# ─── Phase 6: Initramfs ─────────────────────────────────────────────
phase "Phase 6: Building initramfs"

bash "${KAG_REPO_ROOT}/scripts/build-initramfs.sh" 2>&1 | tee -a "$LOG_FILE"
ok "initramfs built"

# ─── Phase 7: Sanity check ──────────────────────────────────────────
if [[ "$SKIP_SANITY" -eq 1 ]]; then
  warn "Skipping sanity check per --skip-sanity"
else
  phase "Phase 7: Sanity check — build & boot defconfig kernel"
  if bash "${KAG_REPO_ROOT}/scripts/sanity-check.sh" 2>&1 | tee -a "$LOG_FILE"; then
    ok "Sanity check passed — kernel boots under QEMU"
  else
    die "Sanity check failed. See $LOG_FILE"
  fi
fi

# ─── Phase 8: Validate subsystem prompt resolution ──────────────────
phase "Phase 8: Validating subsystem configuration"

docker run --rm \
  -v "${KAG_REPO_ROOT}/framework:/opt/framework:ro" \
  -v "${KAG_REPO_ROOT}/subsystems:/opt/subsystems:ro" \
  -e PYTHONPATH=/opt \
  -e KAG_SUBSYSTEM="$KAG_SUBSYSTEM" \
  kag-base python3 -c "
from framework.agents.common.prompt import resolve_prompt
from framework.agents.common.tools import resolve_tools
from framework.subsystem import load_subsystem, list_subsystems
sub = load_subsystem('$KAG_SUBSYSTEM')
print(f'Active subsystem: {sub.display_name} ({sub.name})')
print(f'Available: {list_subsystems()}')
for role in ['planner','implementer','reviewer','tester']:
    content, source = resolve_prompt(role, '$KAG_SUBSYSTEM')
    tools = resolve_tools(role, sub)
    print(f'  {role:12s}  prompt={len(content):5d}c  tools={len(tools)}  <- {source}')
" 2>&1 | tee -a "$LOG_FILE"

# ─── Done ───────────────────────────────────────────────────────────
cat <<EOF

${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
${GREEN}✓ kernel-agents deployed successfully${NC}

Workspace:    ${KAG_WORKSPACE}
Subsystem:    ${KAG_SUBSYSTEM}
Platform:     ${PLATFORM}/${ARCH}
KVM:          $(check_kvm_available && echo "available (hardware accel)" || echo "not available (TCG)")
Logs:         ${KAG_REPO_ROOT}/logs/
Config:       ~/.kag.env

Quick start:
  echo "<task description>" | ./bin/new-task.sh "<title>"

Inspect state:
  ./bin/status.sh

Validate deployment:
  ./bin/dry-run.sh

Uninstall:
  ./bin/cleanup.sh
${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
EOF
