"""Tester agent entrypoint."""
from __future__ import annotations
import json
import sys
from framework.agents.common.harness import read_ticket, write_result, run_agent_loop


def main():
    ticket = read_ticket()
    feedback = ticket.get("iteration_feedback", "")

    branch = ticket.get("branch_impl", "")
    worktree_path = ticket.get("worktree_path", "")

    initial = f"""Task: {ticket['title']}

Branch to test: {branch}
Worktree path: {worktree_path}
Subsystem: {ticket.get('subsystem', 'mm')}
Task ID: {ticket.get('task_id', 'unknown')}
"""
    if feedback:
        initial += f"""
Previous test feedback (fix these):
{feedback}
"""
    initial += f"""
Run the tiered test pipeline: build, boot, KUnit, LTP.

CRITICAL: Use the worktree path '{worktree_path}' for all build operations.
Pass it as the `worktree` argument to make_image, make_defconfig, make_file, etc.
Do NOT pass the branch name as the worktree — it must be a full filesystem path.

Output JSON: {{"status": "ok|failed|skipped", "results": [...], "run_id": "<id>"}}
"""
    result = run_agent_loop("tester", ticket, initial)
    write_result(result)


if __name__ == "__main__":
    main()
