#!/usr/bin/env bash
# bin/status.sh — Inspect kernel-agents installation state.
set -euo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
load_config

echo "=== kernel-agents status ==="
echo
echo "Repo:        $KAG_REPO_ROOT"
echo "Version:     $(cat "$KAG_REPO_ROOT/VERSION" 2>/dev/null || echo unknown)"
echo "Platform:    $(detect_platform) / $(detect_arch)"
echo "Docker:      $(docker --version 2>/dev/null || echo 'NOT INSTALLED')"

if [[ "$(detect_platform)" == "linux" ]]; then
  if check_kvm_available; then
    echo "KVM:         available (Tester uses hardware acceleration)"
  else
    echo "KVM:         not available (Tester uses TCG)"
  fi
else
  if check_virtiofs; then
    echo "VirtioFS:    enabled (good bind-mount perf)"
  else
    echo "VirtioFS:    not detected (Docker 29.x may not report it — check Docker Desktop Settings)"
  fi
  echo "KVM:         not available on macOS containers (Tester uses TCG)"
fi

echo "Workspace:   $KAG_WORKSPACE"
echo "Subsystem:   $KAG_SUBSYSTEM"
echo "Config:      $([[ -f ~/.kag.env ]] && echo '~/.kag.env (present)' || echo 'defaults')"

echo
echo "=== Host ==="
echo "OS:          $(uname -s) $(uname -r)"
echo "Arch:        $(uname -m)"
if [[ "$(detect_platform)" == "linux" ]]; then
  echo "RAM:         $(free -h | awk '/^Mem:/{print $2}')"
  echo "Disk free:   $(df -h "$HOME" | awk 'NR==2 {print $4}')"
else
  echo "RAM:         $(sysctl -n hw.memsize | awk '{print int($1/1073741824)"GB"}')"
  echo "Disk free:   $(df -h "$HOME" | awk 'NR==2 {print $4}')"
fi

echo
echo "=== Docker ==="
docker info >/dev/null 2>&1 && echo "Daemon:      running" || echo "Daemon:      NOT RUNNING"

echo
echo "=== Workspace ==="
[[ -d "$KAG_WORKSPACE/src/.git" ]] && \
  echo "linux.git:   present ($(git -C "$KAG_WORKSPACE/src" log -1 --format='%h %s' 2>/dev/null | head -c 60))" || \
  echo "linux.git:   MISSING"
[[ -f "$KAG_WORKSPACE/initramfs/initramfs.cpio.gz" ]] && \
  echo "initramfs:   present ($(du -h "$KAG_WORKSPACE/initramfs/initramfs.cpio.gz" 2>/dev/null | cut -f1))" || \
  echo "initramfs:   MISSING"
[[ -f "$KAG_WORKSPACE/state/orchestrator.db" ]] && \
  echo "State DB:    present" || echo "State DB:    MISSING"

echo
echo "=== Docker images ==="
for img in kag-base kag-planner kag-implementer kag-reviewer kag-tester kag-archivist; do
  if docker image inspect "$img" >/dev/null 2>&1; then
    SIZE=$(docker image inspect "$img" --format '{{.Size}}' 2>/dev/null | awk '{print int($1/1048576)"MB"}')
    echo "  ✓ $img ($SIZE)"
  else
    echo "  ✗ $img (not built)"
  fi
done

echo
echo "=== Docker volumes ==="
for vol in "$KAG_VOLUME_CCACHE" "$KAG_VOLUME_BUILD"; do
  if docker volume inspect "$vol" >/dev/null 2>&1; then
    echo "  ✓ $vol"
  else
    echo "  ✗ $vol (not created)"
  fi
done

echo
echo "=== Recent tasks ==="
if [[ -f "$KAG_WORKSPACE/state/orchestrator.db" ]]; then
  sqlite3 -header -column "$KAG_WORKSPACE/state/orchestrator.db" \
    "SELECT substr(task_id,1,8) AS id, subsystem, substr(title,1,40) AS title,
            status, iteration
     FROM tickets ORDER BY updated_at DESC LIMIT 5" 2>/dev/null || echo "  (no tasks yet)"
else
  echo "  (state DB not initialized)"
fi

echo
echo "=== LLM costs (last 7 days) ==="
if [[ -f "$KAG_WORKSPACE/state/llm_costs.jsonl" ]]; then
  python3 -c "
import json
from collections import defaultdict
from datetime import datetime, timedelta
cutoff = datetime.now() - timedelta(days=7)
totals = defaultdict(float)
counts = defaultdict(int)
for line in open('$KAG_WORKSPACE/state/llm_costs.jsonl'):
    try:
        d = json.loads(line)
        if datetime.fromisoformat(d['timestamp']) > cutoff:
            totals[d.get('agent','?')] += d.get('cost_usd', 0)
            counts[d.get('agent','?')] += 1
    except: pass
if not totals:
    print('  (no cost data in last 7 days)')
else:
    for agent in sorted(totals.keys()):
        print(f'  {agent:12s}  \${totals[agent]:.4f}  ({counts[agent]} calls)')
    print(f'  {\"TOTAL\":12s}  \${sum(totals.values()):.4f}')
" 2>/dev/null || echo "  (no cost data)"
else
  echo "  (no cost data — run a task first)"
fi
