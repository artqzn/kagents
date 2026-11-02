"""MM-specific extra tools for the Tester agent.

DAMON tests: uses the boot-based KUnit approach (same as run_kunit in
kunit.py) — boots the pre-built kernel with kunit.filter_glob=damon
and parses KTAP from dmesg.
"""
from __future__ import annotations
import os
import uuid
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd_streaming, WORKSPACE, find_kernel_image


def extra_tools(subsystem) -> list[Tool]:
    """Register MM-specific Tester tools."""
    return [
        Tool(
            name="run_damon_suite",
            description=(
                "Run DAMON KUnit tests (mm/damon) by booting the pre-built "
                "kernel with kunit.filter_glob=damon. Parses KTAP from dmesg."
            ),
            fn=_run_damon,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string",
                                 "description": "Worktree path (from ticket) — used to find kernel Image"},
                    "timeout": {"type": "integer", "default": 600},
                },
            },
        ),
    ]


def _detect_qemu_accel_args() -> list[str]:
    accel = os.environ.get("KAG_QEMU_ACCEL", "tcg")
    if accel == "kvm":
        return ["-enable-kvm", "-cpu", "host"]
    else:
        return ["-accel", "tcg,thread=multi", "-cpu", "cortex-a72"]


def _find_kernel(worktree: str = None) -> str | None:
    found = find_kernel_image(worktree)
    return str(found) if found else None


def _run_damon(worktree: str = None, timeout: int = 600, **kwargs) -> dict:
    """Run DAMON KUnit tests by booting the pre-built kernel.

    Uses kunit.filter_glob=damon to run only DAMON-related suites.
    Same approach as run_kunit in kunit.py — no kunit.py, no source
    tree clean requirement.
    """
    kernel = _find_kernel(worktree)
    if not kernel:
        return {
            "ok": False,
            "error": "No kernel Image found. Run make_image first.",
        }

    initrd = "/workspace/initramfs/initramfs.cpio.gz"
    if not Path(initrd).exists():
        return {"ok": False, "error": f"Initramfs not found: {initrd}"}

    kernel_args = [
        "console=ttyAMA0",
        "earlycon=pl011,0x09000000",
        "kunit.filter_glob=damon*",
        "kunit_shutdown=poweroff",
        "rdinit=/init",
    ]

    qemu_cmd = [
        "qemu-system-aarch64",
        "-machine", "virt",
        "-nodefaults",
        *_detect_qemu_accel_args(),
        "-smp", "2", "-m", "2G",
        "-kernel", kernel,
        "-initrd", initrd,
        "-append", " ".join(kernel_args),
        "-nographic", "-no-reboot",
        "-serial", "mon:stdio",
    ]

    run_id = str(uuid.uuid4())[:8]
    log_dir = WORKSPACE / "artifacts" / "test-results" / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(log_dir / "damon_kunit.log")

    result = run_cmd_streaming(
        qemu_cmd, timeout=timeout, prefix="    ",
        log_file=log_path,
    )
    result["run_id"] = run_id
    return result
