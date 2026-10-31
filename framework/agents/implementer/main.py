"""Implementer agent entrypoint."""
from __future__ import annotations
import json
import sys
from framework.agents.common.harness import read_ticket, write_result, run_agent_loop


def main():
    ticket = read_ticket()
    feedback = ticket.get("iteration_feedback", "")

    # The Planner should have returned the worktree_path in the ticket.
    # If not, derive it from the branch name.
    branch = ticket.get("branch_impl", "")
    worktree_path = ticket.get("worktree_path", "")
    if not worktree_path and branch:
        # Default worktree path pattern
        import os
        workspace = os.environ.get("KAG_WORKSPACE", "/workspace")
        worktree_path = f"{workspace}/branches/impl/{branch}"

    initial = f"""Task: {ticket['title']}

RFC document: {ticket.get('rfc_doc', '(missing — ask Planner)')}
Branch: {branch}
Worktree path: {worktree_path}
Iteration: {ticket.get('iteration', 0)}
Subsystem: {ticket.get('subsystem', 'mm')}
Task ID: {ticket.get('task_id', 'unknown')}
"""
    if feedback:
        initial += f"""
Previous review/test feedback (fix these):
{feedback}
"""
    initial += f"""
IMPORTANT: Use the worktree path '{worktree_path}' for all file operations
(edit_file, make_file, git_commit). This is your working directory.

Implement the patch series described in the RFC.

Output JSON: {{"ok": true, "branch": "{branch}", "commits": ["<sha1>", ...]}}
"""
    result = run_agent_loop("implementer", ticket, initial)
    write_result(result)


if __name__ == "__main__":
    main()
