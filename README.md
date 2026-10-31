# kernel-agents

Generic multi-agent framework for Linux kernel subsystem development.
Pluggable profiles per subsystem (mm, sched, block, fs). Runs on macOS
Apple Silicon and Linux (arm64 and x86_64) via Docker containers.

## Quick Start

### macOS (Apple Silicon)

```bash
git clone <repo-url> kernel-agents
cd kernel-agents
cp config/example.env ~/.kag.env
# Edit ~/.kag.env: add your DeepSeek API key
./bin/deploy.sh
```

**Prerequisites:** Docker Desktop with VirtioFS enabled
(Settings → General → "Choose file sharing implementation" → VirtioFS).

### Linux (arm64 or x86_64)

```bash
git clone <repo-url> kernel-agents
cd kernel-agents
cp config/example.env ~/.kag.env
# Edit ~/.kag.env: add your DeepSeek API key
./bin/deploy.sh
```

**Prerequisites:** Docker CE or Docker Engine.
**Optional:** `sudo usermod -aG kvm $USER` (then log out/in) for 10x
faster kernel tests via KVM hardware acceleration.

## Start a Task

```bash
echo "Convert page_zone() callers to folio_zone() in mm/page_alloc.c" | \
  ./bin/new-task.sh "folio-migration-page-alloc"
```

Switch to a different subsystem:

```bash
echo "Add sched_ext BPF scheduler for batch workloads" | \
  KAG_SUBSYSTEM=sched ./bin/new-task.sh "sched-ext-batch"
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `./bin/deploy.sh` | One-shot installer. Validates prerequisites, creates workspace, builds images, runs sanity check. |
| `./bin/cleanup.sh [--all]` | Removes containers, images, volumes. `--all` also removes workspace. |
| `./bin/status.sh` | Shows installation state, recent tasks, LLM costs. |
| `./bin/dry-run.sh` | Non-destructive validator. 17 checks in under 2 minutes. |
| `./bin/new-task.sh "<title>"` | Start a new task. Reads description from stdin. |
| `./bin/resume-task.sh <task-id>` | Resume a failed/interrupted task. |
| `./bin/diagnostics.sh` | Produce a diagnostic bundle for support. |
| `./tests/run_golden.sh` | Run golden test patches through the pipeline. |

## How It Works

```
Task description
    ↓
[Planner] → writes RFC, creates branch
    ↓
[Implementer] → writes code, commits patch series
    ↓
[Reviewer] → runs checkpatch/sparse/smatch, reviews diff
    ↓ (retry with feedback if blockers found)
[Tester] → builds kernel, boots QEMU, runs KUnit/LTP
    ↓ (retry with feedback if tests fail)
Merged
```

Four LLM-driven agents coordinated by a LangGraph state machine. Each
agent runs in its own Docker container. All state persisted to SQLite.

## Supported Subsystems

| Subsystem | Status | Switch with |
|-----------|--------|-------------|
| mm (Memory Management) | Fully implemented (default) | `KAG_SUBSYSTEM=mm` |
| sched (Scheduler) | Stub — extend before use | `KAG_SUBSYSTEM=sched` |
| block (Block I/O) | Stub — extend before use | `KAG_SUBSYSTEM=block` |
| fs (Filesystem) | Stub — extend before use | `KAG_SUBSYSTEM=fs` |

To add a new subsystem, create a directory under `subsystems/` with a
`profile.py` file. See the MM profile (`subsystems/mm/profile.py`) as a
reference. Zero framework code changes required.

## LLM Configuration

Default: DeepSeek (deepseek-chat for Planner/Implementer/Tester,
deepseek-reasoner for Reviewer). Swap per-agent in `~/.kag.env`:

```bash
# Use Anthropic for the Reviewer (better bug-spotting)
KAG_LLM_PROVIDER_REVIEWER=anthropic
KAG_LLM_MODEL_REVIEWER=anthropic/claude-sonnet-4-5
ANTHROPIC_API_KEY=sk-ant-...
```

Any LiteLLM-supported provider works: DeepSeek, Anthropic, OpenAI, Z.ai,
local models via Ollama.

## Uninstall

```bash
./bin/cleanup.sh --all --yes
```

## Documentation

- [Design Document](docs/design-document.pdf) — comprehensive 50-page design spec
- `subsystems/mm/` — reference subsystem profile
- `framework/` — generic framework code (zero subsystem-specific logic)

## License

Apache 2.0
