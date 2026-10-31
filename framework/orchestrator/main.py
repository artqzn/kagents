"""Orchestrator entrypoint.

Reads task title from KAG_TASK_TITLE env var (preferred) or argv[1],
and description from stdin. Creates a Ticket, runs the LangGraph pipeline,
and prints the final state as JSON.
"""
from __future__ import annotations
import sys
import json
import uuid
import subprocess
import os
from pathlib import Path

from framework.config import AGENT_TIMEOUT, DOCKER_COMPOSE, SUBSYSTEM_NAME
from framework.orchestrator.schemas import Ticket
from framework.orchestrator.persist import save_ticket, init_db, load_ticket
from framework.orchestrator.graph import build_graph

# ANSI colors for logging
_R = '\033[0m'
_BOLD = '\033[1m'
_GREEN = '\033[32m'
_RED = '\033[31m'
_YELLOW = '\033[33m'
_GRAY = '\033[90m'

def _log(msg: str):
    """Print to stderr with flush."""
    print(msg, file=sys.stderr, flush=True)


def run_agent(role: str, ticket: Ticket) -> dict:
    """Invoke an agent container with the ticket on stdin.

    Uses docker compose to run the agent service. The agent reads JSON
    from stdin and writes JSON to stdout. Stderr is streamed to the
    terminal in real time so the user can see progress.
    """
    if not DOCKER_COMPOSE:
        return {
            "ok": True,
            "status": "ok",
            "rfc_doc": None,
            "branch": None,
            "comments": [],
            "results": [],
        }

    compose_file = os.environ.get(
        "KAG_DOCKER_COMPOSE",
        "/workspace/docker-compose-resolved.yml"
    )

    # Build docker compose command.
    cmd = [
        "docker", "compose",
        "-f", compose_file,
        "run", "--rm",
        "-e", f"KAG_SUBSYSTEM={ticket.subsystem}",
        role,
    ]

    # Also apply KVM override if it exists (Linux with /dev/kvm)
    kvm_override = Path(compose_file).parent / "compose.kvm.yml"
    if kvm_override.exists() and os.path.exists("/dev/kvm"):
        cmd = [
            "docker", "compose",
            "-f", compose_file,
            "-f", str(kvm_override),
            "run", "--rm",
            "-e", f"KAG_SUBSYSTEM={ticket.subsystem}",
            role,
        ]

    # Print colorful banner
    _R = '\033[0m'
    _BOLD = '\033[1m'
    _CYAN = '\033[36m'
    _GRAY = '\033[90m'
    _GREEN = '\033[32m'
    _RED = '\033[31m'
    _YELLOW = '\033[33m'

    def _color(c, t):
        return f"{c}{t}{_R}" if sys.stderr.isatty() else t

    line = '═' * 70
    print(f"\n{_color(_BOLD, _color(_CYAN, line))}", file=sys.stderr, flush=True)
    print(f"{_color(_BOLD, _color(_CYAN, f'  ▶  STARTING {role.upper()} AGENT'))}",
          file=sys.stderr, flush=True)
    print(f"{_color(_CYAN, f'     Task: {ticket.task_id}')}", file=sys.stderr, flush=True)
    print(f"{_color(_CYAN, f'     Subsystem: {ticket.subsystem}')}", file=sys.stderr, flush=True)
    if ticket.iteration > 0:
        print(f"{_color(_YELLOW, f'     Iteration: {ticket.iteration} (retry)')}",
              file=sys.stderr, flush=True)
    if ticket.iteration_feedback:
        feedback_preview = ticket.iteration_feedback[:150].replace('\n', ' ')
        print(f"{_color(_GRAY, f'     Feedback: {feedback_preview}...')}",
              file=sys.stderr, flush=True)
    print(f"{_color(_BOLD, _color(_CYAN, line))}", file=sys.stderr, flush=True)

    try:
        # Use Popen to separate stdout (captured for JSON) from
        # stderr (streamed to terminal in real time)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # Inherit stderr — streams to terminal
            text=True,
        )
        stdout_data, _ = proc.communicate(
            input=ticket.model_dump_json(),
            timeout=AGENT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"\n  ✗ {role} timed out after {AGENT_TIMEOUT}s",
              file=sys.stderr, flush=True)
        return {
            "ok": False,
            "status": "failed",
            "error": f"Agent {role} timed out after {AGENT_TIMEOUT}s",
        }

    if proc.returncode != 0:
        print(f"\n  ✗ {role} exited with code {proc.returncode}",
              file=sys.stderr, flush=True)
        return {
            "ok": False,
            "status": "failed",
            "error": f"Agent {role} exited {proc.returncode}",
        }

    # Parse JSON result from stdout
    try:
        agent_result = json.loads(stdout_data)
        status = agent_result.get("ok", False) or \
                 agent_result.get("status", "failed")
        _R = '\033[0m'
        _GREEN = '\033[32m'
        _RED = '\033[31m'
        _GRAY = '\033[90m'

        def _c(color, text):
            return f"{color}{text}{_R}" if sys.stderr.isatty() else text

        is_success = status in (True, "ok", "merged")
        icon = _c(_GREEN, '✓') if is_success else _c(_RED, '✗')
        print(f"\n  {icon} {_c(_GREEN if is_success else _RED, f'{role} completed: {status}')}",
              file=sys.stderr, flush=True)
        print(f"  {_c(_GRAY, f'Result: {json.dumps(agent_result, default=str)[:400]}')}",
              file=sys.stderr, flush=True)
        return agent_result
    except json.JSONDecodeError:
        print(f"\n  {_c(_RED, '✗')} {_c(_RED, f'{role} produced non-JSON output')}",
              file=sys.stderr, flush=True)
        print(f"  {_c(_GRAY, f'Raw: {stdout_data[:400]}')}",
              file=sys.stderr, flush=True)
        return {
            "ok": False,
            "status": "failed",
            "error": f"Agent {role} produced non-JSON output",
            "raw": stdout_data[-2000:] if stdout_data else "",
        }


def main():
    init_db()

    # Check for resume via env var (KAG_RESUME=1, KAG_RESUME_TASK_ID=<id>)
    if os.environ.get("KAG_RESUME") == "1":
        task_id = os.environ.get("KAG_RESUME_TASK_ID", "")
        if not task_id:
            print(json.dumps({"error": "KAG_RESUME=1 but KAG_RESUME_TASK_ID not set"}))
            sys.exit(1)
        ticket = load_ticket(task_id)
        if ticket is None:
            print(json.dumps({"error": f"Task {task_id} not found"}))
            sys.exit(1)
        print(f"Resuming task {task_id} (status={ticket.status})", file=sys.stderr)

        # If the task failed at test phase, skip back to test
        # (planner and implementer already succeeded)
        if ticket.status == "failed":
            if ticket.branch_impl and ticket.worktree_path:
                # Plan and impl succeeded — retry from the failed phase
                if ticket.test_results and any(r.get("status") == "fail" for r in ticket.test_results):
                    # Failed at test — retry test
                    ticket.status = "test"
                    ticket.iteration = 0
                    _log(f"  Resuming at TEST phase (plan+impl succeeded)")
                elif ticket.review_comments:
                    # Failed at review — retry review
                    ticket.status = "review"
                    ticket.iteration = 0
                    _log(f"  Resuming at REVIEW phase (plan+impl succeeded)")
                else:
                    # Failed at impl or plan — restart from plan
                    ticket.status = "planning"
                    _log(f"  Resuming from PLAN phase")
            else:
                # No branch/worktree — restart from plan
                ticket.status = "planning"
                _log(f"  Resuming from PLAN phase (no branch/worktree)")
            save_ticket(ticket)
    else:
        # New task: read title from env var (preferred) or argv[1]
        title = os.environ.get("KAG_TASK_TITLE") or \
                (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "untitled")
        description = sys.stdin.read()

        ticket = Ticket(
            task_id=str(uuid.uuid4())[:8],
            title=title,
            description=description,
            subsystem=SUBSYSTEM_NAME,
        )
        save_ticket(ticket)
        print(f"Starting task: {ticket.task_id} (title={title}, subsystem={SUBSYSTEM_NAME})",
              file=sys.stderr)

    # Build and run the graph
    app = build_graph(run_agent_fn=run_agent)

    initial_state = {"ticket": ticket, "last_error": None}
    final_state = app.invoke(initial_state)

    # Save final state
    save_ticket(final_state["ticket"])

    # Print result
    print(json.dumps(final_state, indent=2, default=str))


if __name__ == "__main__":
    main()
