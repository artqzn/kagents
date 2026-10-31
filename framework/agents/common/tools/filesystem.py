"""Filesystem tools — read_file, edit_file (scoped to subsystem)."""
from __future__ import annotations
from pathlib import Path
from framework.agents.common.tools.base import Tool, WORKSPACE, get_worktree_path


def tools(subsystem) -> list[Tool]:
    """Create filesystem tools scoped to the given subsystem."""

    def _validate_scope(path: str) -> tuple[bool, str]:
        """Validate that path is in subsystem scope and inside worktree."""
        # Check subsystem scope
        out_of_scope = subsystem.validate_patch_scope([path])
        if out_of_scope:
            return False, (
                f"File '{path}' is outside {subsystem.name} subsystem scope. "
                f"Allowed paths: {subsystem.source_paths[:3]}..."
            )
        return True, ""

    def read_file(path: str, worktree: str = None) -> dict:
        """Read a file's contents."""
        base = Path(worktree) if worktree else WORKSPACE / "src"
        full = (base / path).resolve()
        if not full.exists():
            return {"error": f"File not found: {path}"}
        try:
            content = full.read_text(errors="replace")
            # Truncate to prevent context bloat — LLM doesn't need 50KB
            if len(content) > 5000:
                content = content[:5000] + \
                    f"\n... (truncated, file is {len(content)} chars total)"
            return {"path": str(full), "content": content}
        except Exception as e:
            return {"error": f"Cannot read file: {e}"}

    def edit_file(path: str, old_str: str, new_str: str,
                  worktree: str = None) -> dict:
        """Edit a file with structured find-replace."""
        base = Path(worktree) if worktree else WORKSPACE / "src"
        full = (base / path).resolve()

        # Validate scope
        ok, msg = _validate_scope(path)
        if not ok:
            return {"error": msg}

        if not full.exists():
            return {"error": f"File not found: {path}"}

        try:
            content = full.read_text()
        except Exception as e:
            return {"error": f"Cannot read file: {e}"}

        if old_str not in content:
            return {"error": "old_str not found in file"}

        new_content = content.replace(old_str, new_str, 1)
        full.write_text(new_content)
        return {"ok": True, "path": str(full)}

    def write_file(path: str, content: str, worktree: str = None) -> dict:
        """Write content to a file (scoped to subsystem)."""
        base = Path(worktree) if worktree else WORKSPACE / "src"
        full = (base / path).resolve()

        ok, msg = _validate_scope(path)
        if not ok:
            return {"error": msg}

        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        return {"ok": True, "path": str(full)}

    def list_files(path: str = ".", worktree: str = None) -> dict:
        """List files in a directory."""
        base = Path(worktree) if worktree else WORKSPACE / "src"
        full = (base / path).resolve()
        if not full.exists():
            return {"error": f"Directory not found: {path}"}
        if not full.is_dir():
            return {"error": f"Not a directory: {path}"}
        entries = []
        for p in sorted(full.iterdir()):
            entries.append({
                "name": p.name,
                "type": "dir" if p.is_dir() else "file",
                "size": p.stat().st_size if p.is_file() else 0,
            })
        return {"path": str(full), "entries": entries}

    return [
        Tool(
            name="read_file",
            description="Read a file's contents. Path is relative to worktree or src/.",
            fn=read_file,
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'mm/page_alloc.c'"},
                    "worktree": {"type": "string", "description": "Optional worktree path"},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="edit_file",
            description="Edit a file with structured find-replace. Path must be in subsystem scope.",
            fn=edit_file,
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                    "worktree": {"type": "string"},
                },
                "required": ["path", "old_str", "new_str"],
            },
        ),
        Tool(
            name="write_file",
            description="Write content to a file. Path must be in subsystem scope.",
            fn=write_file,
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "worktree": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        ),
        Tool(
            name="list_files",
            description="List files in a directory.",
            fn=list_files,
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path, default '.'"},
                    "worktree": {"type": "string"},
                },
            },
        ),
    ]
