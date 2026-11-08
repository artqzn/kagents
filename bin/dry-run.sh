#!/usr/bin/env bash
# bin/dry-run.sh — Non-destructive deployment validator.
#
# Validates that every layer of kernel-agents is correctly configured
# without performing expensive operations. Safe to run repeatedly.
#
# Usage:
#   ./bin/dry-run.sh                  # full check
#   ./bin/dry-run.sh --skip-llm       # skip LLM connectivity check
#   ./bin/dry-run.sh --skip-build     # skip single-file build check
#   ./bin/dry-run.sh --skip-qemu      # skip QEMU boot check
#   ./bin/dry-run.sh --json           # machine-readable output
#
# Exit codes:
#   0  all checks passed
#   1  one or more checks failed
set -uo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
migrate_legacy_config
load_config

# ─── Args ───────────────────────────────────────────────────────────
SKIP_LLM=0
SKIP_BUILD=0
SKIP_QEMU=0
JSON_OUTPUT=0
for arg in "$@"; do
  case "$arg" in
    --skip-llm)   SKIP_LLM=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --skip-qemu)  SKIP_QEMU=1 ;;
    --json)       JSON_OUTPUT=1 ;;
    -h|--help)    sed -n '2,18p' "$0"; exit 0 ;;
    *) die "Unknown arg: $arg (try --help)" ;;
  esac
done

# ─── State ──────────────────────────────────────────────────────────
TMPDIR_DRY=$(mktemp -d -t kag-dryrun-XXXXXX)
trap 'rm -rf "$TMPDIR_DRY"' EXIT

RESULTS_FILE="$TMPDIR_DRY/results.txt"
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

# ─── Output helpers ─────────────────────────────────────────────────
if [[ "$JSON_OUTPUT" -eq 1 ]]; then
  log() { :; }
  ok()  { :; }
  warn(){ :; }
  phase(){ :; }
else
  log()   { echo "  $*" >&2; }
  ok()    { echo "${GREEN}  ✓${NC} $*" >&2; }
  warn()  { echo "${YELLOW}  !${NC} $*" >&2; }
  phase() { echo; echo "${BLUE}▶ $*${NC}" >&2; }
fi

record() {
  local status="$1" name="$2" detail="$3"
  echo "${status}|${name}|${detail}" >> "$RESULTS_FILE"
  if [[ "$JSON_OUTPUT" -eq 0 ]]; then
    case "$status" in
      PASS) echo "${GREEN}  [PASS]${NC} ${name}" >&2 ;;
      FAIL) echo "${RED}  [FAIL]${NC} ${name} — ${detail}" >&2 ;;
      SKIP) echo "${YELLOW}  [SKIP]${NC} ${name} — ${detail}" >&2 ;;
    esac
  fi
  case "$status" in
    PASS) ((PASS_COUNT++)) ;;
    FAIL) ((FAIL_COUNT++)) ;;
    SKIP) ((SKIP_COUNT++)) ;;
  esac
}

check_pass() { record PASS "$1" "$2"; }
check_fail() { record FAIL "$1" "$2"; }
check_skip() { record SKIP "$1" "$2"; }

# ═══════════════════════════════════════════════════════════════════
# CHECK 1: Host prerequisites
# ═══════════════════════════════════════════════════════════════════
phase "Check 1: Host prerequisites"

PLATFORM=$(detect_platform)
ARCH=$(detect_arch)

case "$PLATFORM" in
  macos|linux) check_pass "host.os" "$PLATFORM" ;;
  *)           check_fail "host.os" "Unsupported: $PLATFORM" ;;
esac

case "$ARCH" in
  arm64|x86_64) check_pass "host.arch" "$ARCH" ;;
  *)            check_fail "host.arch" "Unsupported: $ARCH" ;;
esac

case "$PLATFORM/$ARCH" in
  macos/x86_64) check_fail "host.combo" "Intel Mac not supported" ;;
  *)            check_pass "host.combo" "$PLATFORM/$ARCH supported" ;;
esac

if command -v git >/dev/null; then
  check_pass "host.git" "$(git --version | head -1)"
else
  check_fail "host.git" "git not installed"
fi

if [[ "$PLATFORM" == "linux" ]]; then
  MEM_GB=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}')
else
  MEM_GB=$(sysctl -n hw.memsize 2>/dev/null | awk '{print int($1/1073741824)}')
fi
if [[ "${MEM_GB:-0}" -ge 16 ]]; then
  check_pass "host.ram" "${MEM_GB}GB"
else
  check_fail "host.ram" "Found ${MEM_GB:-unknown}GB. 16GB required."
fi

if [[ "$PLATFORM" == "linux" ]]; then
  DISK_FREE_GB=$(df -BG "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' | tr -d 'G')
else
  DISK_FREE_GB=$(df -g "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')
fi
if [[ "${DISK_FREE_GB:-0}" -ge 50 ]]; then
  check_pass "host.disk" "${DISK_FREE_GB}GB free"
else
  check_fail "host.disk" "Only ${DISK_FREE_GB:-unknown}GB free. 50GB+ required."
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 2: Docker
# ═══════════════════════════════════════════════════════════════════
phase "Check 2: Docker"

if ! command -v docker >/dev/null; then
  check_fail "docker.binary" "docker not in PATH. Install Docker."
else
  check_pass "docker.binary" "$(docker --version)"
fi

if docker info >/dev/null 2>&1; then
  check_pass "docker.daemon" "running"
else
  check_fail "docker.daemon" "not running. Start Docker daemon."
fi

if [[ "$PLATFORM" == "macos" ]]; then
  if check_virtiofs; then
    check_pass "docker.virtiofs" "VirtioFS enabled (good bind-mount perf)"
  else
    # Not a hard failure — Docker 29.x may not report VirtioFS even when active.
    # Deploy's sanity check (kernel boot speed) is the definitive test.
    check_pass "docker.virtiofs" "not detected via docker info (Docker 29.x may not report it — deploy will verify)"
  fi
elif [[ "$PLATFORM" == "linux" ]]; then
  check_pass "docker.runtime" "Docker native on Linux — bind-mount perf is native"
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 2.5: Platform capabilities (KVM, cross-compile)
# ═══════════════════════════════════════════════════════════════════
phase "Check 2.5: Platform capabilities"

check_pass "platform.os" "$PLATFORM"
check_pass "platform.arch" "$ARCH"

if [[ "$PLATFORM" == "linux" ]]; then
  if check_kvm_available; then
    check_pass "platform.kvm" "/dev/kvm accessible — Tester will use KVM"
  else
    check_pass "platform.kvm" "not accessible — Tester will use TCG (slower but functional)"
  fi
elif [[ "$PLATFORM" == "macos" ]]; then
  check_pass "platform.kvm" "not available on macOS containers — TCG only (expected)"
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 3: Configuration
# ═══════════════════════════════════════════════════════════════════
phase "Check 3: Configuration"

if [[ -f "${HOME}/.kag.env" ]]; then
  check_pass "config.user" "~/.kag.env present"
else
  check_pass "config.user" "no ~/.kag.env (using defaults — OK for testing)"
fi

HAS_KEY=0
for var in DEEPSEEK_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY ZAI_API_KEY; do
  if [[ -n "${!var:-}" ]]; then HAS_KEY=1; break; fi
done
if [[ "$HAS_KEY" -eq 1 ]]; then
  check_pass "config.apikey" "at least one provider key set"
else
  check_fail "config.apikey" "no LLM API key in env. Add to ~/.kag.env"
fi

if [[ -f "${KAG_REPO_ROOT}/subsystems/${KAG_SUBSYSTEM}/profile.py" ]]; then
  check_pass "config.subsystem" "${KAG_SUBSYSTEM}"
else
  check_fail "config.subsystem" "${KAG_SUBSYSTEM} profile not found"
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 4: Docker images
# ═══════════════════════════════════════════════════════════════════
phase "Check 4: Docker images"

EXPECTED_IMAGES=(kag-base kag-planner kag-implementer kag-reviewer kag-tester kag-archivist)
IMAGES_PRESENT=0
IMAGES_MISSING=0
for img in "${EXPECTED_IMAGES[@]}"; do
  if docker image inspect "$img" >/dev/null 2>&1; then
    ((IMAGES_PRESENT++))
  else
    ((IMAGES_MISSING++))
  fi
done
if [[ "$IMAGES_MISSING" -eq 0 ]]; then
  check_pass "images.all" "${IMAGES_PRESENT}/${#EXPECTED_IMAGES[@]} present"
else
  check_fail "images.all" "${IMAGES_MISSING}/${#EXPECTED_IMAGES[@]} missing. Run ./bin/deploy.sh"
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 5: Docker volumes
# ═══════════════════════════════════════════════════════════════════
phase "Check 5: Docker volumes"

for vol in "$KAG_VOLUME_CCACHE"; do
  if docker volume inspect "$vol" >/dev/null 2>&1; then
    check_pass "volume.${vol}" "present"
  else
    check_fail "volume.${vol}" "missing. Run ./bin/deploy.sh"
  fi
done

# ═══════════════════════════════════════════════════════════════════
# CHECK 6: Workspace
# ═══════════════════════════════════════════════════════════════════
phase "Check 6: Workspace"

if [[ -d "$KAG_WORKSPACE" ]]; then
  check_pass "workspace.dir" "$KAG_WORKSPACE"
else
  check_fail "workspace.dir" "$KAG_WORKSPACE does not exist. Run ./bin/deploy.sh"
fi

MISSING_DIRS=()
for d in branches/impl \
         artifacts docs/rfcs initramfs state; do
  if [[ ! -d "${KAG_WORKSPACE}/${d}" ]]; then
    MISSING_DIRS+=("$d")
  fi
done
if [[ ${#MISSING_DIRS[@]} -eq 0 ]]; then
  check_pass "workspace.layout" "all required dirs present"
else
  check_fail "workspace.layout" "missing: ${MISSING_DIRS[*]}"
fi

if [[ -d "${KAG_WORKSPACE}/src/.git" ]]; then
  COMMIT=$(git -C "${KAG_WORKSPACE}/src" log -1 --format='%h %s' 2>/dev/null | head -c 60)
  check_pass "workspace.linux_git" "$COMMIT"
else
  check_fail "workspace.linux_git" "linux.git not cloned at ${KAG_WORKSPACE}/src"
fi

if [[ -f "${KAG_WORKSPACE}/initramfs/initramfs.cpio.gz" ]]; then
  SIZE=$(du -h "${KAG_WORKSPACE}/initramfs/initramfs.cpio.gz" 2>/dev/null | cut -f1)
  check_pass "workspace.initramfs" "${SIZE}"
else
  check_fail "workspace.initramfs" "missing. Run scripts/build-initramfs.sh"
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 7: Subsystem profile loads
# ═══════════════════════════════════════════════════════════════════
phase "Check 7: Subsystem profile"

if [[ "$IMAGES_MISSING" -gt 0 ]]; then
  check_skip "subsystem.profile" "kag-base image not built"
else
  PROFILE_CHECK=$(docker run --rm \
    -v "${KAG_REPO_ROOT}/framework:/opt/framework:ro" \
    -v "${KAG_REPO_ROOT}/subsystems:/opt/subsystems:ro" \
    -e KAG_SUBSYSTEM="$KAG_SUBSYSTEM" \
    -e PYTHONPATH=/opt \
    kag-base python3 -c "
import sys
try:
    from framework.subsystem import load_subsystem, list_subsystems
    sub = load_subsystem('$KAG_SUBSYSTEM')
    print(f'OK|{sub.display_name}|{sub.commit_prefix}|{len(sub.source_paths)}paths|{len(sub.kconfig_overrides)}kconfigs')
    print('AVAILABLE:' + ','.join(list_subsystems()))
except Exception as e:
    print(f'FAIL|{e}')
    sys.exit(1)
" 2>&1)

  if [[ "$PROFILE_CHECK" == OK* ]]; then
    IFS='|' read -r _ display prefix paths kconfigs _ <<< "$PROFILE_CHECK"
    check_pass "subsystem.profile" "${display} (${prefix}, ${paths}, ${kconfigs})"
  else
    check_fail "subsystem.profile" "$PROFILE_CHECK"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 8: Prompt resolution
# ═══════════════════════════════════════════════════════════════════
phase "Check 8: Prompt resolution"

if [[ "$IMAGES_MISSING" -gt 0 ]]; then
  check_skip "prompt.all" "kag-base image not built"
else
  PROMPT_CHECK=$(docker run --rm \
    -v "${KAG_REPO_ROOT}/framework:/opt/framework:ro" \
    -v "${KAG_REPO_ROOT}/subsystems:/opt/subsystems:ro" \
    -e KAG_SUBSYSTEM="$KAG_SUBSYSTEM" \
    -e PYTHONPATH=/opt \
    kag-base python3 -c "
from framework.agents.common.prompt import resolve_prompt
for role in ['planner', 'implementer', 'reviewer', 'tester']:
    try:
        content, source = resolve_prompt(role, '$KAG_SUBSYSTEM')
        print(f'{role}|OK|{len(content)}|{source}')
    except Exception as e:
        print(f'{role}|FAIL|{e}')
" 2>&1)

  while IFS='|' read -r role status len source; do
    [[ -z "$role" ]] && continue
    if [[ "$status" == "OK" ]]; then
      check_pass "prompt.${role}" "${len} chars"
    else
      check_fail "prompt.${role}" "$status"
    fi
  done <<< "$PROMPT_CHECK"
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 9: Tool resolution
# ═══════════════════════════════════════════════════════════════════
phase "Check 9: Tool resolution"

if [[ "$IMAGES_MISSING" -gt 0 ]]; then
  check_skip "tools.all" "kag-base image not built"
else
  TOOL_CHECK=$(docker run --rm \
    -v "${KAG_REPO_ROOT}/framework:/opt/framework:ro" \
    -v "${KAG_REPO_ROOT}/subsystems:/opt/subsystems:ro" \
    -e KAG_SUBSYSTEM="$KAG_SUBSYSTEM" \
    -e PYTHONPATH=/opt \
    kag-base python3 -c "
from framework.subsystem import load_subsystem
from framework.agents.common.tools import resolve_tools
sub = load_subsystem('$KAG_SUBSYSTEM')
for role in ['planner', 'implementer', 'reviewer', 'tester']:
    try:
        tools = resolve_tools(role, sub)
        names = ','.join(t.name for t in tools)
        print(f'{role}|OK|{len(tools)}|{names[:80]}')
    except Exception as e:
        print(f'{role}|FAIL|{e}')
" 2>&1)

  while IFS='|' read -r role status count names; do
    [[ -z "$role" ]] && continue
    if [[ "$status" == "OK" ]]; then
      check_pass "tools.${role}" "${count} tools"
    else
      check_fail "tools.${role}" "$status"
    fi
  done <<< "$TOOL_CHECK"
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 10: State DB
# ═══════════════════════════════════════════════════════════════════
phase "Check 10: State DB"

if [[ "$IMAGES_MISSING" -gt 0 ]]; then
  check_skip "state.db" "kag-base image not built"
else
  DB_CHECK=$(docker run --rm \
    -v "${KAG_REPO_ROOT}/framework:/opt/framework:ro" \
    -e PYTHONPATH=/opt \
    -e KAG_WORKSPACE="/tmp/dry-workspace" \
    kag-base python3 -c "
import os
os.makedirs('/tmp/dry-workspace/state', exist_ok=True)
from framework.orchestrator.persist import init_db, save_ticket, load_ticket
from framework.orchestrator.schemas import Ticket
init_db()
t = Ticket(task_id='dryrun01', title='dry run test', description='test', subsystem='$KAG_SUBSYSTEM')
save_ticket(t)
loaded = load_ticket('dryrun01')
if loaded and loaded.task_id == 'dryrun01':
    print('OK|init+save+load works')
else:
    print('FAIL|round-trip failed')
" 2>&1)

  if [[ "$DB_CHECK" == OK* ]]; then
    check_pass "state.db" "init + save + load round-trip"
  else
    check_fail "state.db" "$DB_CHECK"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 11: Orchestrator graph
# ═══════════════════════════════════════════════════════════════════
phase "Check 11: Orchestrator graph"

if [[ "$IMAGES_MISSING" -gt 0 ]]; then
  check_skip "orchestrator.graph" "kag-base image not built"
else
  GRAPH_CHECK=$(docker run --rm \
    -v "${KAG_REPO_ROOT}/framework:/opt/framework:ro" \
    -e PYTHONPATH=/opt \
    kag-base python3 -c "
try:
    from framework.orchestrator.graph import build_graph
    app = build_graph()
    nodes = list(app.get_graph().nodes.keys())
    print(f'OK|nodes={nodes}')
except Exception as e:
    print(f'FAIL|{e}')
" 2>&1)

  if [[ "$GRAPH_CHECK" == OK* ]]; then
    check_pass "orchestrator.graph" "${GRAPH_CHECK#OK|}"
  else
    check_fail "orchestrator.graph" "$GRAPH_CHECK"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 12: LLM connectivity
# ═══════════════════════════════════════════════════════════════════
phase "Check 12: LLM connectivity"

if [[ "$SKIP_LLM" -eq 1 ]]; then
  check_skip "llm.connectivity" "skipped per --skip-llm"
elif [[ "$HAS_KEY" -eq 0 ]]; then
  check_skip "llm.connectivity" "skipped (no API key)"
elif [[ "$IMAGES_MISSING" -gt 0 ]]; then
  check_skip "llm.connectivity" "kag-base image not built"
else
  LLM_CHECK=$(docker run --rm \
    -v "${KAG_REPO_ROOT}/framework:/opt/framework:ro" \
    -e KAG_LLM_MODEL_PLANNER="${KAG_LLM_MODEL_PLANNER}" \
    -e KAG_LLM_MAX_TOKENS_PLANNER=20 \
    -e DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}" \
    -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
    -e OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
    -e ZAI_API_KEY="${ZAI_API_KEY:-}" \
    kag-base python3 -c "
import os
from litellm import completion
try:
    r = completion(
        model=os.environ['KAG_LLM_MODEL_PLANNER'],
        messages=[{'role':'user','content':'Reply with: PONG'}],
        max_tokens=20,
    )
    content = r.choices[0].message.content.strip()
    cost = r.usage.prompt_tokens + r.usage.completion_tokens
    print(f'OK|{content}|{cost}tokens|{r.model}')
except Exception as e:
    print(f'FAIL|{e}')
" 2>&1)

  if [[ "$LLM_CHECK" == OK* ]]; then
    check_pass "llm.connectivity" "${LLM_CHECK#OK|}"
  else
    check_fail "llm.connectivity" "$LLM_CHECK"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 13: Single-file build (uses kag-tester, not kag-base)
# ═══════════════════════════════════════════════════════════════════
phase "Check 13: Toolchain (single-file build)"

if [[ "$SKIP_BUILD" -eq 1 ]]; then
  check_skip "build.toolchain" "skipped per --skip-build"
elif [[ ! -d "${KAG_WORKSPACE}/src/.git" ]]; then
  check_skip "build.toolchain" "linux.git not present"
elif ! docker image inspect kag-tester >/dev/null 2>&1; then
  check_skip "build.toolchain" "kag-tester image not built"
else
  BUILD_CHECK=$($(get_timeout_cmd 120) docker run --rm \
    -v "${KAG_WORKSPACE}/src:/workspace/src:cached" \
    -v "${KAG_VOLUME_CCACHE}:/root/.ccache" \
    -e ARCH=arm64 -e LLVM=1 \
    kag-tester bash -c '
      cd /workspace/src
      [[ -f .config ]] || make defconfig >/dev/null 2>&1
      make -j4 scripts/basic/fixdep 2>&1 | tail -3
      if [[ -x scripts/basic/fixdep ]]; then
          echo "OK|fixdep built"
      else
          echo "FAIL|fixdep not produced"
      fi
    ' 2>&1)

  if echo "$BUILD_CHECK" | grep -q "^OK|"; then
    check_pass "build.toolchain" "single-file build works"
  else
    check_fail "build.toolchain" "$(echo "$BUILD_CHECK" | tail -1)"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 14: QEMU TCG
# ═══════════════════════════════════════════════════════════════════
phase "Check 14: QEMU"

if [[ "$SKIP_QEMU" -eq 1 ]]; then
  check_skip "qemu" "skipped per --skip-qemu"
elif ! docker image inspect kag-tester >/dev/null 2>&1; then
  check_skip "qemu" "kag-tester image not built"
else
  if ! docker run --rm kag-tester which qemu-system-aarch64 >/dev/null 2>&1; then
    check_fail "qemu.binary" "qemu-system-aarch64 not in kag-tester image"
  else
    QEMU_VER=$(docker run --rm kag-tester qemu-system-aarch64 --version 2>&1 | head -1)
    if [[ "$QEMU_VER" == *"QEMU"* ]]; then
      check_pass "qemu.binary" "$QEMU_VER"
    else
      check_fail "qemu.binary" "QEMU binary broken"
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 15: Agent container contract
# ═══════════════════════════════════════════════════════════════════
phase "Check 15: Agent container contract"

if docker image inspect kag-planner >/dev/null 2>&1; then
  CONTRACT_CHECK=$(echo '{"task_id":"dryrun01","title":"dryrun","description":"Reply with: {\"ok\":true}","subsystem":"'"$KAG_SUBSYSTEM"'"}' | \
    $(get_timeout_cmd 60) docker run --rm -i \
      --entrypoint python3 \
      -v "${KAG_REPO_ROOT}/framework:/opt/framework:ro" \
      -v "${KAG_REPO_ROOT}/subsystems:/opt/subsystems:ro" \
      -v "${KAG_WORKSPACE}:/workspace:cached" \
      -e AGENT_ROLE=planner \
      -e KAG_SUBSYSTEM="$KAG_SUBSYSTEM" \
      -e PYTHONPATH=/opt \
      -e KAG_LLM_MODEL_PLANNER="${KAG_LLM_MODEL_PLANNER}" \
      -e KAG_LLM_MAX_TOKENS_PLANNER=100 \
      -e DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}" \
      -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
      kag-planner /opt/framework/agents/planner/main.py 2>&1 || true)

  if echo "$CONTRACT_CHECK" | grep -q '"ok"'; then
    check_pass "agent.contract" "container reads stdin, writes JSON stdout"
  else
    check_fail "agent.contract" "container did not produce JSON"
  fi
else
  check_skip "agent.contract" "kag-planner image not built"
fi

# ═══════════════════════════════════════════════════════════════════
# CHECK 16: Subsystem scoping
# ═══════════════════════════════════════════════════════════════════
phase "Check 16: Subsystem scoping"

if [[ "$IMAGES_MISSING" -gt 0 ]]; then
  check_skip "subsystem.scoping" "kag-base image not built"
else
  SCOPE_CHECK=$(docker run --rm \
    -v "${KAG_REPO_ROOT}/framework:/opt/framework:ro" \
    -v "${KAG_REPO_ROOT}/subsystems:/opt/subsystems:ro" \
    -e KAG_SUBSYSTEM="$KAG_SUBSYSTEM" \
    -e PYTHONPATH=/opt \
    kag-base python3 -c "
from framework.subsystem import load_subsystem
sub = load_subsystem('$KAG_SUBSYSTEM')
in_scope_sample = sub.source_paths[0] if sub.source_paths else 'mm/page_alloc.c'
out_of_scope = ['drivers/gpu/drm/foo.c', 'net/sched/bar.c', 'fs/ext4/baz.c']
result_in = sub.validate_patch_scope([in_scope_sample])
result_out = sub.validate_patch_scope(out_of_scope)
if result_in == [] and len(result_out) == 3:
    print(f'OK|in-scope accepted, out-of-scope rejected')
else:
    print(f'FAIL|in={result_in}, out={result_out}')
" 2>&1)

  if [[ "$SCOPE_CHECK" == OK* ]]; then
    check_pass "subsystem.scoping" "scope validation works"
  else
    check_fail "subsystem.scoping" "$SCOPE_CHECK"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════
TOTAL=$((PASS_COUNT + FAIL_COUNT + SKIP_COUNT))

if [[ "$JSON_OUTPUT" -eq 1 ]]; then
  python3 -c "
import json
results = []
for line in open('$RESULTS_FILE'):
    parts = line.rstrip().split('|', 2)
    if len(parts) == 3:
        status, name, detail = parts
        results.append({'status': status, 'name': name, 'detail': detail})
print(json.dumps({
    'summary': {'pass': $PASS_COUNT, 'fail': $FAIL_COUNT, 'skip': $SKIP_COUNT, 'total': $TOTAL},
    'checks': results,
}, indent=2))
"
  exit $((FAIL_COUNT > 0 ? 1 : 0))
fi

echo
echo "${BLUE}═══════════════════════════════════════════════════════════════════${NC}"
if [[ "$FAIL_COUNT" -eq 0 ]]; then
  echo "${GREEN}✓ Dry-run passed: ${PASS_COUNT} passed, ${SKIP_COUNT} skipped, 0 failed${NC}"
  echo
  echo "Deployment is correctly configured. Safe to run real tasks."
  EXIT_CODE=0
else
  echo "${RED}✗ Dry-run failed: ${PASS_COUNT} passed, ${SKIP_COUNT} skipped, ${FAIL_COUNT} failed${NC}"
  echo
  echo "Failed checks:"
  grep '^FAIL' "$RESULTS_FILE" | while IFS='|' read -r _ name detail; do
    echo "  ${RED}•${NC} ${name}: ${detail}"
  done
  echo
  echo "Fix the failures above, then re-run: ./bin/dry-run.sh"
  EXIT_CODE=1
fi
echo "${BLUE}═══════════════════════════════════════════════════════════════════${NC}"

exit $EXIT_CODE
