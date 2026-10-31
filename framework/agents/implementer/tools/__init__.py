"""Implementer tools — filesystem, git, build."""
from __future__ import annotations
from framework.agents.common.tools.filesystem import tools as fs_tools
from framework.agents.common.tools.gitops import tools as git_tools
from framework.agents.common.tools.build import tools as build_tools


def tools(subsystem) -> list:
    """Implementer gets: read_file, edit_file, write_file, git ops, build tools."""
    all_tools = []
    # Filesystem: all tools
    all_tools.extend(fs_tools(subsystem))
    # Git: all except create_branch
    for t in git_tools(subsystem):
        if t.name != "create_branch":
            all_tools.append(t)
    # Build: all
    all_tools.extend(build_tools(subsystem))
    return all_tools
