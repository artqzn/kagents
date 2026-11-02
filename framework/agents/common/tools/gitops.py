"""Git operations tools."""
from __future__ import annotations
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd, WORKSPACE, get_worktree_path


def tools(subsystem) -> list[Tool]:
    """Create git tools."""

    def _remove_stale_lock(repo: str) -> None:
        """Remove stale git index.lock if it exists."""
        lock_file = Path(repo) / ".git" / "index.lock"
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass

    def _resolve_repo(repo: str = None) -> str:
        """Resolve repo path — use provided path, or fall back to /workspace/src.

        If the provided path doesn't exist, fall back to /workspace/src.
        """
        if not repo or repo == "None" or repo == ".":
            return str(WORKSPACE / "src")
        p = Path(repo)
        if not p.is_absolute():
            p = WORKSPACE / p
        resolved = str(p)
        if not Path(resolved).exists():
            return str(WORKSPACE / "src")
        return resolved

    def git_log(n: int = 20, path: str = None, repo: str = None) -> dict:
        """Show recent git log."""
        repo_path = _resolve_repo(repo)
        _remove_stale_lock(repo_path)
        cmd = ["git", "-C", repo_path, "log", f"-{n}", "--oneline"]
        if path:
            cmd += ["--", path]
        return run_cmd(cmd, timeout=30)

    def git_diff(branch: str = "HEAD", repo: str = None) -> dict:
        """Show git diff."""
        repo_path = _resolve_repo(repo)
        _remove_stale_lock(repo_path)
        return run_cmd(
            ["git", "-C", repo_path, "diff", branch],
            timeout=120,
        )

    def git_diff_stat(base: str = "HEAD", repo: str = None) -> dict:
        """Show diffstat between base and HEAD."""
        repo_path = _resolve_repo(repo)
        _remove_stale_lock(repo_path)
        return run_cmd(
            ["git", "-C", repo_path, "diff", "--stat", base],
            timeout=120,
        )

    def create_branch(name: str, from_ref: str = "HEAD",
                      worktree_path: str = None) -> dict:
        """Create a new git worktree with a branch.

        Idempotent: if the branch or worktree already exists, removes them
        first. Always creates the worktree at /workspace/branches/impl/<name>
        regardless of the worktree_path argument (for consistency).

        Note: kernel worktree creation can take 60-90s on macOS with VirtioFS.
        """
        src = str(WORKSPACE / "src")
        _remove_stale_lock(src)
        # Always use the standard path — ignore custom worktree_path
        wt_path = str(WORKSPACE / "branches" / "impl" / name)

        # Clean up any existing worktree at this path (idempotent)
        existing_wt_check = run_cmd(
            ["git", "-C", src, "worktree", "list", "--porcelain"],
            timeout=10,
        )
        if existing_wt_check["ok"] and wt_path in existing_wt_check["stdout"]:
            run_cmd(
                ["git", "-C", src, "worktree", "remove", "--force", wt_path],
                timeout=30,
            )

        # Clean up any existing branch with this name (idempotent)
        branch_check = run_cmd(
            ["git", "-C", src, "show-ref", "--verify", "--quiet",
             f"refs/heads/{name}"],
            timeout=10,
        )
        if branch_check["ok"]:
            run_cmd(
                ["git", "-C", src, "branch", "-D", name],
                timeout=10,
            )

        # Remove any leftover directory
        import shutil
        if Path(wt_path).exists():
            shutil.rmtree(wt_path, ignore_errors=True)

        # Now create the worktree (with longer timeout for kernel tree)
        # Suppress stderr ("Updating files: 32%...") to keep result small
        result = run_cmd(
            ["git", "-C", src, "worktree", "add", "-b", name, wt_path, from_ref],
            timeout=180,
        )
        if result["ok"]:
            # Don't include the verbose "Updating files" stdout/stderr
            result["stdout"] = ""
            result["stderr"] = ""
            result["worktree_path"] = wt_path
            result["branch"] = name
        return result

    def _ensure_git_identity(worktree: str) -> None:
        """Ensure git user identity is configured for the worktree.

        If GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL env vars are set, use those.
        Otherwise, set a default identity in the worktree's git config.
        """
        import os
        name = os.environ.get("GIT_AUTHOR_NAME", "Kernel Agents")
        email = os.environ.get("GIT_AUTHOR_EMAIL", "agents@kernel-agents.local")

        # Check if identity is already configured
        r = run_cmd(
            ["git", "-C", worktree, "config", "user.name"],
            timeout=5,
        )
        if not r["ok"] or not r.get("stdout", "").strip():
            run_cmd(
                ["git", "-C", worktree, "config", "user.name", name],
                timeout=5,
            )
        r = run_cmd(
            ["git", "-C", worktree, "config", "user.email"],
            timeout=5,
        )
        if not r["ok"] or not r.get("stdout", "").strip():
            run_cmd(
                ["git", "-C", worktree, "config", "user.email", email],
                timeout=5,
            )

    def git_commit(message: str, worktree: str, files: list = None,
                   amend: bool = False) -> dict:
        """Stage specific files and commit.

        Args:
            message: Commit message
            worktree: Path to the git worktree
            files: List of specific file paths to stage (relative to worktree).
                   If None, uses `git add -u` (only tracked files that changed).
                   Pass explicit file paths for fastest performance — avoids
                   scanning the entire 80,000-file kernel tree.
            amend: If True, amend the previous commit

        Only stages the files you explicitly list. Never scans the whole tree.
        Automatically ensures git identity is configured.
        """
        if not worktree or worktree == "None":
            return {"ok": False,
                    "error": "worktree argument is required (got None or empty)"}

        _remove_stale_lock(worktree)
        _ensure_git_identity(worktree)

        # Stage files — either explicit list or just tracked changes
        if files:
            # Stage only the specific files the agent modified
            add_cmd = ["git", "-C", worktree, "add"] + files
        else:
            # Fallback: stage only tracked files that changed (not untracked)
            add_cmd = ["git", "-C", worktree, "add", "-u"]

        r_add = run_cmd(add_cmd, timeout=60)
        if not r_add["ok"]:
            return r_add

        cmd = ["git", "-C", worktree, "commit"]
        if amend:
            cmd.append("--amend")
        cmd += ["-m", message]
        return run_cmd(cmd, timeout=60)

    def git_apply(patch: str, worktree: str = None) -> dict:
        """Apply a git patch."""
        repo_path = worktree or str(WORKSPACE / "src")
        _remove_stale_lock(repo_path)
        # Write patch to temp file
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
            f.write(patch)
            patch_file = f.name
        return run_cmd(
            ["git", "-C", repo_path, "apply", patch_file],
            timeout=60,
        )

    def git_changed_files(base: str = "HEAD", repo: str = None) -> dict:
        """List files changed between base and HEAD."""
        repo_path = _resolve_repo(repo)
        _remove_stale_lock(repo_path)
        r = run_cmd(
            ["git", "-C", repo_path, "diff", "--name-only", base],
            timeout=120,
        )
        if r["ok"]:
            files = [f for f in r["stdout"].strip().split("\n") if f]
            return {"ok": True, "files": files}
        return r

    def remove_git_lock(repo: str = None) -> dict:
        """Remove stale git index.lock file.

        Git leaves an index.lock file when a git operation is interrupted.
        This tool removes it so subsequent git operations can proceed.
        The .git directory is normally outside subsystem scope, but this
        tool explicitly allows removing the lock file.
        """
        repo_path = _resolve_repo(repo)
        lock_file = Path(repo_path) / ".git" / "index.lock"
        if lock_file.exists():
            try:
                lock_file.unlink()
                return {"ok": True, "message": f"Removed {lock_file}"}
            except Exception as e:
                return {"ok": False, "error": f"Cannot remove lock: {e}"}
        return {"ok": True, "message": "No lock file present"}

    return [
        Tool(
            name="git_log",
            description="Show recent git log entries.",
            fn=git_log,
            schema={
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "default": 20},
                    "path": {"type": "string"},
                    "repo": {"type": "string"},
                },
            },
        ),
        Tool(
            name="git_diff",
            description="Show git diff for a branch.",
            fn=git_diff,
            schema={
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "default": "HEAD"},
                    "repo": {"type": "string"},
                },
            },
        ),
        Tool(
            name="git_diff_stat",
            description="Show diffstat between base and HEAD.",
            fn=git_diff_stat,
            schema={
                "type": "object",
                "properties": {
                    "base": {"type": "string", "default": "main"},
                    "repo": {"type": "string"},
                },
            },
        ),
        Tool(
            name="create_branch",
            description="Create a new git worktree with a branch.",
            fn=create_branch,
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "from_ref": {"type": "string", "default": "main"},
                    "worktree_path": {"type": "string"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="git_commit",
            description=(
                "Stage specific files and commit in a worktree. "
                "Pass the 'files' argument with a list of file paths you "
                "modified (e.g. ['mm/page_alloc.c']). This is FAST because "
                "it only stages those files, not the whole tree. "
                "If files is omitted, falls back to 'git add -u' (tracked changes only). "
                "NEVER use 'git add -A' or 'git add .' — it scans 80,000+ files."
            ),
            fn=git_commit,
            schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string",
                                "description": "Commit message (multi-line OK)"},
                    "worktree": {"type": "string",
                                 "description": "Path to the git worktree"},
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific files to stage (e.g. ['mm/page_alloc.c']). Pass the files you edited.",
                    },
                    "amend": {"type": "boolean", "default": False},
                },
                "required": ["message", "worktree"],
            },
        ),
        Tool(
            name="git_apply",
            description="Apply a git patch.",
            fn=git_apply,
            schema={
                "type": "object",
                "properties": {
                    "patch": {"type": "string"},
                    "worktree": {"type": "string"},
                },
                "required": ["patch"],
            },
        ),
        Tool(
            name="git_changed_files",
            description="List files changed between base and HEAD.",
            fn=git_changed_files,
            schema={
                "type": "object",
                "properties": {
                    "base": {"type": "string", "default": "main"},
                    "repo": {"type": "string"},
                },
            },
        ),
        Tool(
            name="remove_git_lock",
            description=(
                "Remove stale git index.lock file. Call this if git_commit "
                "or git_apply fails with 'index.lock: File exists'."
            ),
            fn=remove_git_lock,
            schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                },
            },
        ),
    ]
