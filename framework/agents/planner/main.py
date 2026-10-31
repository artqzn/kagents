"""Planner agent entrypoint."""
from __future__ import annotations
import json
import sys
from framework.agents.common.harness import read_ticket, write_result, run_agent_loop


def main():
    ticket = read_ticket()
    task_id = ticket.get('task_id', 'unknown')
    initial = f"""Task: {ticket['title']}

Description:
{ticket['description']}

Subsystem: {ticket.get('subsystem', 'mm')}
Task ID: {task_id}

Produce:
1. RFC document — call write_doc with filename="{task_id}.md" and doc_type="rfcs"
   The tool returns the path. Use that EXACT path in your finish() call.

2. Git worktree — call create_branch with:
   name="agent/planner/{task_id}", from_ref="HEAD"
   The tool returns "worktree_path" in its result. Use that EXACT path.

3. Call the `finish` tool with ALL of these fields:
   finish({{
     "ok": true,
     "rfc_doc": "<the path returned by write_doc>",
     "branch": "agent/planner/{task_id}",
     "worktree_path": "<the worktree_path returned by create_branch>"
   }})

CRITICAL RULES:
- rfc_doc MUST be the exact path returned by write_doc (e.g. "/workspace/docs/rfcs/{task_id}.md")
- worktree_path MUST be the exact path returned by create_branch
  (NOT "/workspace/src" — that's the main repo, not your worktree!)
- branch MUST be "agent/planner/{task_id}"
- Do NOT hardcode paths — always use what the tools return

If you cannot complete the task, call:
  finish({{"ok": false, "error": "<reason>"}})
"""
    result = run_agent_loop("planner", ticket, initial)
    write_result(result)


if __name__ == "__main__":
    main()
