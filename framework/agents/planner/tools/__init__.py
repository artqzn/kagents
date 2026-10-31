"""Planner tools — read, search, write docs, create branches."""
from __future__ import annotations
from framework.agents.common.tools.filesystem import tools as fs_tools
from framework.agents.common.tools.gitops import tools as git_tools
from framework.agents.common.tools.docs import tools as doc_tools


def tools(subsystem) -> list:
    """Planner gets: read_file, list_files, git_log, write_doc, create_branch."""
    all_tools = []
    # Filesystem: read-only tools (read_file, list_files)
    for t in fs_tools(subsystem):
        if t.name in ("read_file", "list_files"):
            all_tools.append(t)
    # Git: log and branch creation
    for t in git_tools(subsystem):
        if t.name in ("git_log", "create_branch", "git_changed_files"):
            all_tools.append(t)
    # Docs: write and read
    all_tools.extend(doc_tools(subsystem))
    return all_tools
