"""Reviewer agent entrypoint."""
from __future__ import annotations
import json
import sys
from framework.agents.common.harness import read_ticket, write_result, run_agent_loop


def main():
    ticket = read_ticket()
    branch = ticket.get("branch_impl", "")
    worktree_path = ticket.get("worktree_path", "")

    initial = f"""Task: {ticket['title']}

Branch to review: {branch}
Worktree path: {worktree_path}
Subsystem: {ticket.get('subsystem', 'mm')}
Task ID: {ticket.get('task_id', 'unknown')}

Review the patch series on this branch. For each issue found, output a
comment with: file, line, severity (blocker|major|minor|nit), comment,
and optional suggested_fix.

CRITICAL: Use the worktree path '{worktree_path}' for all git operations
(git_log, git_diff, git_changed_files). Do NOT use "." or "/workspace"
as the repo path — those are not git repositories.

Output JSON: {{"status": "ok|blocked|failed", "comments": [...], "error": null}}
"""
    result = run_agent_loop("reviewer", ticket, initial)
    write_result(result)


if __name__ == "__main__":
    main()
