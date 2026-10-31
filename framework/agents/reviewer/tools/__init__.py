"""Reviewer tools — static analysis + read-only git/filesystem."""
from __future__ import annotations
from framework.agents.common.tools.filesystem import tools as fs_tools
from framework.agents.common.tools.gitops import tools as git_tools
from framework.agents.common.tools.analyze import tools as analyze_tools


def tools(subsystem) -> list:
    """Reviewer gets: read_file, list_files, git diff/log, analyze tools."""
    all_tools = []
    # Filesystem: read-only
    for t in fs_tools(subsystem):
        if t.name in ("read_file", "list_files"):
            all_tools.append(t)
    # Git: diff and log
    for t in git_tools(subsystem):
        if t.name in ("git_log", "git_diff", "git_diff_stat",
                      "git_changed_files"):
            all_tools.append(t)
    # Analysis: all
    all_tools.extend(analyze_tools(subsystem))
    return all_tools
