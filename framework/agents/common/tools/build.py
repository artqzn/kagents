"""Build tools — make commands for kernel compilation."""
from __future__ import annotations
import os
import platform
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd, run_cmd_streaming, WORKSPACE


def tools(subsystem) -> list[Tool]:
    """Create build tools."""

    def _resolve_worktree(worktree: str = None) -> str:
        """Resolve worktree path to an absolute path.

        If worktree is None or "None", use /workspace/src.
        If it's a relative path, resolve it relative to /workspace.
        If the resolved path doesn't exist, fall back to /workspace/src.
        """
        if not worktree or worktree == "None":
            return str(WORKSPACE / "src")
        p = Path(worktree)
        if not p.is_absolute():
            p = WORKSPACE / p
        resolved = str(p)
        # If the worktree doesn't exist, fall back to /workspace/src
        if not Path(resolved).exists():
            src = str(WORKSPACE / "src")
            if Path(src).exists():
                return src
        return resolved

    def _make_cmd(target: str, arch: str = None, jobs: int = None,
                  cross_compile: bool = None) -> list[str]:
        """Build a make command with ccache, proper arch and cross-compile."""
        arch = arch or os.environ.get("KAG_ARCH", "arm64")

        if cross_compile is None:
            host_arch = platform.machine()
            cross_compile = (
                host_arch == "x86_64" and arch == "arm64"
            )

        cmd = ["make"]
        if cross_compile:
            cmd.append("CROSS_COMPILE=aarch64-linux-gnu-")
        cmd.append(f"ARCH={arch}")
        if os.environ.get("KAG_LLVM", "1") == "1":
            cmd.append("LLVM=1")
            # Wrap clang with ccache for faster incremental builds.
            # ccache is shared across all tasks via the kag-ccache volume.
            # First build: cold cache (~15-30 min). Subsequent: warm (~1-5 min).
            cmd.append('CC=ccache clang')
        cmd.append(f"-j{jobs or os.cpu_count() or 4}")
        cmd.append(target)
        return cmd

    def make_image(worktree: str = None, arch: str = None) -> dict:
        """Build the kernel Image with spinner and log file.

        Build output goes to a log file, not the terminal.
        A spinner shows progress in the terminal.
        Timeout is 1800s (30 min) to handle cold builds on macOS.
        """
        import uuid
        cwd = _resolve_worktree(worktree)
        cmd = _make_cmd("Image", arch=arch)

        # Log file for build output
        run_id = str(uuid.uuid4())[:8]
        log_path = str(WORKSPACE / "artifacts" / "test-results" / run_id / "build.log")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)

        result = run_cmd_streaming(
            cmd, timeout=1800, cwd=cwd, prefix="    ",
            log_file=log_path,
        )
        result["run_id"] = run_id
        return result

    def make_modules(worktree: str = None, arch: str = None) -> dict:
        """Build kernel modules."""
        cwd = _resolve_worktree(worktree)
        cmd = _make_cmd("modules", arch=arch)
        return run_cmd(cmd, timeout=900, cwd=cwd)

    def make_file(filepath: str, worktree: str = None,
                  arch: str = None) -> dict:
        """Build a single object file (fast inner loop)."""
        cwd = _resolve_worktree(worktree)
        if filepath.endswith(".c"):
            obj = filepath[:-2] + ".o"
        else:
            obj = filepath
        cmd = _make_cmd(obj, arch=arch)
        return run_cmd(cmd, timeout=120, cwd=cwd)

    def make_defconfig(worktree: str = None, arch: str = None) -> dict:
        """Run make defconfig."""
        cwd = _resolve_worktree(worktree)
        cmd = _make_cmd("defconfig", arch=arch)
        return run_cmd(cmd, timeout=60, cwd=cwd)

    def make_olddefconfig(worktree: str = None, arch: str = None) -> dict:
        """Run make olddefconfig."""
        cwd = _resolve_worktree(worktree)
        cmd = _make_cmd("olddefconfig", arch=arch)
        return run_cmd(cmd, timeout=60, cwd=cwd)

    return [
        Tool(
            name="make_image",
            description=(
                "Build the kernel Image (full build, 5-15 min). "
                "Pass worktree=<worktree_path from ticket>."
            ),
            fn=make_image,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string",
                                 "description": "Full path to worktree (from ticket)"},
                    "arch": {"type": "string", "default": "arm64"},
                },
            },
        ),
        Tool(
            name="make_modules",
            description="Build kernel modules.",
            fn=make_modules,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string"},
                    "arch": {"type": "string"},
                },
            },
        ),
        Tool(
            name="make_file",
            description="Build a single .o file (fast inner loop, <1s).",
            fn=make_file,
            schema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string",
                                 "description": "e.g. 'mm/page_alloc.c'"},
                    "worktree": {"type": "string"},
                    "arch": {"type": "string"},
                },
                "required": ["filepath"],
            },
        ),
        Tool(
            name="make_defconfig",
            description="Run make defconfig to generate default config.",
            fn=make_defconfig,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string"},
                    "arch": {"type": "string"},
                },
            },
        ),
        Tool(
            name="make_olddefconfig",
            description="Run make olddefconfig after .config changes.",
            fn=make_olddefconfig,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string"},
                    "arch": {"type": "string"},
                },
            },
        ),
    ]
