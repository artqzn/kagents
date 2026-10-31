"""Static analysis tools — checkpatch, sparse, smatch, coccinelle.

All tools gracefully skip if the analyzer is not installed, returning
{"ok": true, "skipped": true} instead of failing. This prevents the
reviewer from wasting iterations on unavailable tools.
"""
from __future__ import annotations
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd, WORKSPACE
import shutil


def tools(subsystem) -> list[Tool]:
    """Create static analysis tools."""

    def _check_tool(name: str) -> bool:
        """Check if a tool is available in PATH."""
        return shutil.which(name) is not None

    def _ensure_config(cwd: str) -> None:
        """Ensure .config exists in the kernel source directory."""
        if not Path(cwd, ".config").exists():
            run_cmd(["make", "ARCH=arm64", "LLVM=1", "defconfig"],
                    timeout=60, cwd=cwd)

    def _resolve_cwd(worktree: str = None) -> str:
        """Resolve worktree path for analysis tools."""
        if not worktree or worktree == "None":
            return str(WORKSPACE / "src")
        p = Path(worktree)
        if not p.is_absolute():
            p = WORKSPACE / p
        resolved = str(p)
        if not Path(resolved).exists():
            return str(WORKSPACE / "src")
        return resolved

    def run_checkpatch(patch_file: str = None, worktree: str = None) -> dict:
        """Run checkpatch.pl on a patch file."""
        cwd = _resolve_cwd(worktree)
        checkpatch = Path(cwd) / "scripts" / "checkpatch.pl"

        if not checkpatch.exists():
            return {"ok": False,
                    "error": f"checkpatch.pl not found at {checkpatch}"}

        if not patch_file:
            return {"ok": False, "error": "patch_file required"}

        return run_cmd(
            ["bash", str(checkpatch), "--strict", "-f", patch_file],
            timeout=60, cwd=cwd,
        )

    def run_checkpatch_diff(diff: str, worktree: str = None) -> dict:
        """Run checkpatch.pl on a diff string."""
        import tempfile
        cwd = _resolve_cwd(worktree)
        checkpatch = Path(cwd) / "scripts" / "checkpatch.pl"

        if not checkpatch.exists():
            return {"ok": False,
                    "error": f"checkpatch.pl not found at {checkpatch}"}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".patch",
                                         delete=False) as f:
            f.write(diff)
            patch_file = f.name
        return run_cmd(
            ["bash", str(checkpatch), "--strict", "-f", patch_file],
            timeout=60, cwd=cwd,
        )

    def run_sparse(file: str, worktree: str = None) -> dict:
        """Run sparse on a source file.

        Skips if not installed or if .config is missing.
        Timeout is 30s. Warnings are treated as OK.
        """
        if not _check_tool("sparse"):
            return {"ok": True, "skipped": True,
                    "reason": "sparse not installed"}

        cwd = _resolve_cwd(worktree)
        if not Path(cwd, ".config").exists():
            return {"ok": True, "skipped": True,
                    "reason": "No .config (run make_defconfig first)"}

        result = run_cmd(
            ["sparse", "-I", f"{cwd}/include", "-I", f"{cwd}/arch/arm64/include",
             file],
            timeout=30, cwd=cwd,
        )
        # Sparse returns non-zero on warnings — treat as OK
        if not result["ok"] and "warning:" in result.get("stdout", ""):
            result["ok"] = True
        return result

    def run_smatch(file: str, worktree: str = None) -> dict:
        """Run smatch on a source file.

        Skips if not installed or if .config is missing.
        Timeout is 30s. Findings are treated as OK (we want the output).
        """
        if not _check_tool("smatch"):
            return {"ok": True, "skipped": True,
                    "reason": "smatch not installed"}

        cwd = _resolve_cwd(worktree)
        if not Path(cwd, ".config").exists():
            return {"ok": True, "skipped": True,
                    "reason": "No .config (run make_defconfig first)"}

        result = run_cmd(
            ["smatch", "--full-path", file],
            timeout=30, cwd=cwd,
        )
        # Smatch returns non-zero on findings — treat as OK
        if not result["ok"] and result.get("stdout", "").strip():
            result["ok"] = True
        return result

    def run_coccinelle(spatch_file: str, worktree: str = None) -> dict:
        """Run coccinelle (spatch) with a semantic patch.

        Skips gracefully if not installed.
        """
        if not _check_tool("spatch"):
            return {"ok": True, "skipped": True,
                    "reason": "coccinelle (spatch) not installed"}

        cwd = _resolve_cwd(worktree)
        return run_cmd(
            ["spatch", "--dir", ".", "-D", "rules", spatch_file],
            timeout=300, cwd=cwd,
        )

    return [
        Tool(
            name="run_checkpatch",
            description="Run checkpatch.pl --strict on a patch file.",
            fn=run_checkpatch,
            schema={
                "type": "object",
                "properties": {
                    "patch_file": {"type": "string"},
                    "worktree": {"type": "string",
                                 "description": "Path to worktree (from ticket)"},
                },
                "required": ["patch_file"],
            },
        ),
        Tool(
            name="run_checkpatch_diff",
            description="Run checkpatch.pl on a diff string.",
            fn=run_checkpatch_diff,
            schema={
                "type": "object",
                "properties": {
                    "diff": {"type": "string"},
                    "worktree": {"type": "string"},
                },
                "required": ["diff"],
            },
        ),
        Tool(
            name="run_sparse",
            description="Run sparse on a source file. Skips if not installed.",
            fn=run_sparse,
            schema={
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "worktree": {"type": "string"},
                },
                "required": ["file"],
            },
        ),
        Tool(
            name="run_smatch",
            description="Run smatch on a source file. Skips if not installed.",
            fn=run_smatch,
            schema={
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "worktree": {"type": "string"},
                },
                "required": ["file"],
            },
        ),
        Tool(
            name="run_coccinelle",
            description="Run coccinelle (spatch). Skips if not installed.",
            fn=run_coccinelle,
            schema={
                "type": "object",
                "properties": {
                    "spatch_file": {"type": "string"},
                    "worktree": {"type": "string"},
                },
                "required": ["spatch_file"],
            },
        ),
    ]
