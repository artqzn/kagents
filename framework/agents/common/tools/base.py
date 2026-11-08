"""Tool base class and helpers."""
from __future__ import annotations
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Any, Optional
import json
import os


WORKSPACE = Path(os.environ.get("KAG_WORKSPACE", "/workspace"))


@dataclass
class Tool:
    """A tool that an LLM agent can call."""

    name: str
    description: str
    fn: Callable[..., dict]
    schema: dict

    def __call__(self, **kwargs) -> dict:
        try:
            result = self.fn(**kwargs)
            # Ensure result is JSON-serializable
            if isinstance(result, dict):
                return result
            return {"result": str(result)}
        except Exception as e:
            return {"error": f"Tool '{self.name}' failed: {e}"}

    def to_openai_schema(self) -> dict:
        """Convert to OpenAI/LiteLLM tool schema format.

        Returns:
            {
                "type": "function",
                "function": {
                    "name": ...,
                    "description": ...,
                    "parameters": {  # <-- schema wrapped under "parameters"
                        "type": "object",
                        "properties": {...}
                    }
                }
            }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


def run_cmd(
    cmd: list[str],
    timeout: int = 600,
    cwd: Optional[str] = None,
    capture: bool = True,
    env: Optional[dict] = None,
) -> dict:
    """Run a shell command, return structured result.

    Returns dict with: ok, stdout, stderr, code
    """
    try:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        r = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
            cwd=cwd or str(WORKSPACE),
            env=full_env,
        )
        return {
            "ok": r.returncode == 0,
            "stdout": r.stdout[-8000:] if r.stdout else "",
            "stderr": r.stderr[-4000:] if r.stderr else "",
            "code": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "error": f"Command not found: {cmd[0]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_cmd_streaming(
    cmd: list[str],
    timeout: int = 600,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    prefix: str = "  ",
    log_file: Optional[str] = None,
) -> dict:
    """Run a long-running command with a spinner and log file.

    Output is captured to a log file (not printed to terminal).
    A spinner animation shows progress in the terminal.

    Returns dict with: ok, stdout, stderr, code, duration, lines, log_path
    """
    import subprocess as sp
    import sys
    import time as _time
    import threading

    try:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        _time_start = _time.time()
        line_count = 0

        # Open log file if specified
        log_fh = None
        if log_file:
            from pathlib import Path as _P
            _P(log_file).parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_file, 'w')

        proc = sp.Popen(
            cmd,
            stdout=sp.PIPE,
            stderr=sp.STDOUT,
            text=True,
            cwd=cwd or str(WORKSPACE),
            env=full_env,
            bufsize=1,
        )

        stdout_lines = []
        spinner_chars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
        spinner_idx = 0
        last_spinner_time = _time.time()
        timed_out = False

        def _update_spinner():
            nonlocal spinner_idx, last_spinner_time
            now = _time.time()
            if now - last_spinner_time > 0.1:
                elapsed = now - _time_start
                mins = int(elapsed // 60)
                secs = int(elapsed % 60)
                icon = spinner_chars[spinner_idx % len(spinner_chars)]
                sys.stderr.write(
                    f"\r{prefix}{icon} Running... {mins}m{secs:02d}s "
                    f"({line_count} lines)"
                )
                sys.stderr.flush()
                spinner_idx += 1
                last_spinner_time = now

        try:
            for line in iter(proc.stdout.readline, ''):
                stdout_lines.append(line)
                line_count += 1
                if log_fh:
                    log_fh.write(line)
                    log_fh.flush()

                _update_spinner()

                elapsed = _time.time() - _time_start
                if elapsed > timeout:
                    proc.kill()
                    timed_out = True
                    break

        except Exception:
            pass

        if not timed_out:
            proc.wait()

        if log_fh:
            log_fh.close()

        # Clear spinner line
        sys.stderr.write(f"\r{' ' * 80}\r")
        sys.stderr.flush()

        stdout = ''.join(stdout_lines)
        elapsed = _time.time() - _time_start

        if timed_out:
            return {
                "ok": False,
                "error": f"Timeout after {timeout}s",
                "stdout": stdout[-8000:] if stdout else "",
                "code": -1,
                "duration": elapsed,
                "lines": line_count,
                "log_path": log_file,
            }

        return {
            "ok": proc.returncode == 0,
            "stdout": stdout[-8000:] if stdout else "",
            "stderr": "",
            "code": proc.returncode,
            "duration": elapsed,
            "lines": line_count,
            "log_path": log_file,
        }
    except FileNotFoundError:
        return {"ok": False, "error": f"Command not found: {cmd[0]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_worktree_path(role: str, task_id: str) -> Path:
    """Get the worktree path for a specific agent role and task."""
    role_dir = {
        "planner": "planner",
        "implementer": "impl",
        "reviewer": "reviewer",
        "tester": "tester",
    }.get(role, role)
    return WORKSPACE / "branches" / role_dir / task_id


def find_kernel_image(worktree: str = None) -> Path | None:
    """Find the kernel Image.

    Search order:
    1. /workspace/artifacts/<task-id>/<role>/build/Image  (current agent's build)
    2. /workspace/artifacts/<task-id>/implementer/build/Image  (implementer's build)
    3. /workspace/artifacts/<task-id>/tester/build/Image  (tester's build)
    4. /workspace/artifacts/sanity/build/Image  (sanity check build)
    5. <worktree>/arch/arm64/boot/Image  (in-tree, backward compat)

    Returns the first existing path, or None if not found.
    """
    import os
    candidates = []

    if worktree:
        task_id = Path(worktree).name
        role = os.environ.get("AGENT_ROLE", "unknown")
        # Current agent's build (both normal and kunit variants)
        candidates.append(WORKSPACE / "artifacts" / task_id / role / "build-kunit" / "Image")
        candidates.append(WORKSPACE / "artifacts" / task_id / role / "build" / "Image")
        # Implementer's build
        candidates.append(WORKSPACE / "artifacts" / task_id / "implementer" / "build" / "Image")
        # Tester's build (both variants)
        candidates.append(WORKSPACE / "artifacts" / task_id / "tester" / "build-kunit" / "Image")
        candidates.append(WORKSPACE / "artifacts" / task_id / "tester" / "build" / "Image")

    # Sanity check build (deploy-time verification)
    candidates.append(WORKSPACE / "artifacts" / "sanity" / "build" / "Image")

    # In-tree (backward compat)
    if worktree:
        candidates.append(Path(worktree) / "arch" / "arm64" / "boot" / "Image")

    for c in candidates:
        if c.exists():
            return c
    return None


def find_kernel_config(worktree: str = None) -> Path | None:
    """Find the kernel .config.

    Returns the first existing path, or None if not found.
    """
    import os
    candidates = []

    if worktree:
        task_id = Path(worktree).name
        role = os.environ.get("AGENT_ROLE", "unknown")
        candidates.append(WORKSPACE / "artifacts" / task_id / role / "build-kunit" / ".config")
        candidates.append(WORKSPACE / "artifacts" / task_id / role / "build" / ".config")
        candidates.append(WORKSPACE / "artifacts" / task_id / "implementer" / "build" / ".config")
        candidates.append(WORKSPACE / "artifacts" / task_id / "tester" / "build-kunit" / ".config")
        candidates.append(WORKSPACE / "artifacts" / task_id / "tester" / "build" / ".config")

    # Sanity check
    candidates.append(WORKSPACE / "artifacts" / "sanity" / "build" / ".config")

    # Worktree
    if worktree:
        candidates.append(Path(worktree) / ".config")

    candidates.append(WORKSPACE / "src" / ".config")

    for c in candidates:
        if c.exists():
            return c
    return None
