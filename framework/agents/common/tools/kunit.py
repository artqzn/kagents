"""KUnit test runner tools."""
from __future__ import annotations
import os
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd, WORKSPACE


def tools(subsystem) -> list[Tool]:
    """Create KUnit tools."""

    def run_kunit(worktree: str = None, filter: str = None,
                  timeout: int = 300) -> dict:
        """Run KUnit tests via kunit tool.

        Tries kunit.sh first, then kunit.py. Falls back to /workspace/src
        if not found in the worktree.
        """
        cwd = worktree or str(WORKSPACE / "src")

        # Find kunit script — try worktree, then /workspace/src
        kunit_script = None
        for base_dir in [cwd, str(WORKSPACE / "src")]:
            for script_name in ["kunit.sh", "kunit.py"]:
                candidate = Path(base_dir) / "tools" / "testing" / "kunit" / script_name
                if candidate.exists():
                    kunit_script = candidate
                    cwd = base_dir
                    break
            if kunit_script:
                break

        if not kunit_script:
            return {
                "ok": True,  # Skip, not fail
                "skipped": True,
                "reason": "kunit.sh/kunit.py not found in worktree or /workspace/src",
            }

        if filter is None:
            filter = getattr(subsystem, "kunit_filter", "")

        if not filter:
            return {
                "ok": True,
                "skipped": True,
                "reason": "No KUnit filter for this subsystem",
            }

        # Use python3 for kunit.py, bash for kunit.sh
        if kunit_script.suffix == ".py":
            cmd = [
                "python3", str(kunit_script), "run",
                "--arch=arm64",
                "--make_options=LLVM=1",
                "--kernel_args=kunit.enable=1",
            ]
        else:
            cmd = [
                "bash", str(kunit_script), "run",
                "--arch=arm64",
                "--make_options=LLVM=1",
                "--kernel_args=kunit.enable=1",
            ]

        # Add KVM if available
        if os.environ.get("KAG_QEMU_ACCEL") == "kvm":
            cmd.append("--qemu_args=-enable-kvm -cpu host")

        cmd.append(filter)

        return run_cmd(cmd, timeout=timeout, cwd=cwd)

    def parse_tap(tap_output: str) -> dict:
        """Parse TAP output from KUnit."""
        lines = tap_output.strip().split("\n")
        results = []
        for line in lines:
            line = line.strip()
            if line.startswith("ok ") or line.startswith("not ok "):
                parts = line.split(None, 3)
                if len(parts) >= 3:
                    status = "pass" if parts[0] == "ok" else "fail"
                    test_num = parts[1]
                    test_name = " ".join(parts[2:])
                    results.append({
                        "num": test_num,
                        "name": test_name,
                        "status": status,
                    })
        total = len(results)
        passed = sum(1 for r in results if r["status"] == "pass")
        failed = total - passed
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "results": results,
        }

    return [
        Tool(
            name="run_kunit",
            description=(
                "Run KUnit tests via kunit.sh. Uses subsystem's kunit_filter. "
                "Returns TAP output."
            ),
            fn=run_kunit,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string"},
                    "filter": {"type": "string",
                               "description": "KUnit suite filter (default: subsystem filter)"},
                    "timeout": {"type": "integer", "default": 300},
                },
            },
        ),
        Tool(
            name="parse_tap",
            description="Parse TAP output from KUnit or other test runners.",
            fn=parse_tap,
            schema={
                "type": "object",
                "properties": {
                    "tap_output": {"type": "string"},
                },
                "required": ["tap_output"],
            },
        ),
    ]
