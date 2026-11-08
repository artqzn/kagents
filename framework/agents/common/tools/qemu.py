"""QEMU boot tool with KVM/TCG auto-detection.

Uses run_cmd_streaming so QEMU output goes to a log file with a spinner
in the terminal. Automatically finds the kernel Image in the worktree.
"""
from __future__ import annotations
import os
import uuid
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd_streaming, WORKSPACE, find_kernel_image


def tools(subsystem) -> list[Tool]:
    """Create QEMU tools."""

    def get_qemu_accel_args() -> list[str]:
        """Choose QEMU acceleration based on env.

        Under TCG, use -cpu cortex-a72 instead of -cpu max.
        cortex-a72 is a real ARMv8.0 part with a heavily-optimized TCG
        code path; 'max' enables every possible CPU feature (SVE, SME,
        MTE, SVE2, ...) that TCG must then emulate, making boot 3-5x
        slower. We don't need those features for smoke boot or LTP.
        """
        accel = os.environ.get("KAG_QEMU_ACCEL", "tcg")
        if accel == "kvm":
            return ["-enable-kvm", "-cpu", "host"]
        else:
            return ["-accel", "tcg,thread=multi", "-cpu", "cortex-a72"]

    def _find_kernel(worktree: str = None) -> str | None:
        """Find the kernel Image using the shared helper.

        Searches (in order):
        1. /workspace/artifacts/<task-id>/<role>/build/Image (current agent)
        2. /workspace/artifacts/<task-id>/implementer/build/Image
        3. /workspace/artifacts/<task-id>/tester/build/Image
        4. /workspace/artifacts/sanity/build/Image (sanity check)
        5. <worktree>/arch/arm64/boot/Image (in-tree, backward compat)
        """
        found = find_kernel_image(worktree)
        return str(found) if found else None

    def boot_qemu(kernel: str = None, initrd: str = None,
                  extra_args: list[str] = None, timeout: int = None,
                  memory: str = "2G", smp: str = "4",
                  worktree: str = None) -> dict:
        """Boot a kernel in QEMU and return the boot log.

        If kernel path is not specified, searches the worktree and
        build volume for a kernel Image.
        """
        # Find kernel
        if kernel:
            kernel_path = kernel
        else:
            kernel_path = _find_kernel(worktree)

        if not kernel_path or not Path(kernel_path).exists():
            searched = []
            if worktree:
                task_id = Path(worktree).name
                role = os.environ.get("AGENT_ROLE", "unknown")
                searched.append(f"/workspace/artifacts/{task_id}/{role}/build/Image")
                searched.append(f"/workspace/artifacts/{task_id}/implementer/build/Image")
                searched.append(f"/workspace/artifacts/{task_id}/tester/build/Image")
            searched.append("/workspace/artifacts/sanity/build/Image")
            if worktree:
                searched.append(f"{worktree}/arch/arm64/boot/Image")
            return {"ok": False,
                    "error": f"Kernel not found. Searched: {', '.join(searched)}. "
                             "Pass kernel=<path> or run make_image first."}

        # Find initramfs
        initrd_path = initrd or "/workspace/initramfs/initramfs.cpio.gz"
        if not Path(initrd_path).exists():
            return {"ok": False,
                    "error": f"Initramfs not found: {initrd_path}"}

        # Build kernel args
        kernel_args = [
            "console=ttyAMA0",
            "earlycon=pl011,0x09000000",
            "rdinit=/init",
        ]
        if hasattr(subsystem, "qemu_extra_args"):
            kernel_args.extend(subsystem.qemu_extra_args)
        if extra_args:
            kernel_args.extend(extra_args)

        cmd = [
            "qemu-system-aarch64",
            "-machine", "virt",
            "-nodefaults",  # Don't create default devices (avoids efi-virtio.rom)
            *get_qemu_accel_args(),
            "-smp", smp,
            "-m", memory,
            "-kernel", kernel_path,
            "-initrd", initrd_path,
            "-append", " ".join(kernel_args),
            "-nographic",
            "-no-reboot",
            "-serial", "mon:stdio",
        ]

        timeout = timeout or int(os.environ.get("KAG_QEMU_TIMEOUT_SEC", "60"))

        # Use streaming with log file
        run_id = str(uuid.uuid4())[:8]
        # Per-task test artifacts: artifacts/<task-id>/test/
        task_id = Path(worktree).name if worktree else run_id
        role = os.environ.get("AGENT_ROLE", "unknown")
        log_dir = WORKSPACE / "artifacts" / task_id / role / "test"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / "boot.log")

        result = run_cmd_streaming(
            cmd, timeout=timeout, prefix="    ",
            log_file=log_path,
        )

        # Check for successful boot
        stdout = result.get("stdout", "")
        boot_ok = "MM test kernel booted" in stdout

        return {
            "ok": boot_ok,
            "run_id": run_id,
            "log_path": log_path,
            "kernel_path": kernel_path,
            "stdout": stdout[-4000:],
            "duration": result.get("duration", 0),
            "error": result.get("error"),
        }

    return [
        Tool(
            name="boot_qemu",
            description=(
                "Boot a kernel in QEMU. Auto-finds the kernel Image in the "
                "worktree (arch/arm64/boot/Image) or build volume. "
                "Pass worktree=<path from ticket> to find the built kernel. "
                "Output goes to a log file with a spinner in terminal."
            ),
            fn=boot_qemu,
            schema={
                "type": "object",
                "properties": {
                    "kernel": {"type": "string",
                               "description": "Explicit path to kernel Image (optional — auto-detects)"},
                    "initrd": {"type": "string",
                               "description": "Path to initramfs (optional — uses default)"},
                    "worktree": {"type": "string",
                                 "description": "Worktree path (from ticket) — used to find kernel Image"},
                    "extra_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra kernel cmdline args",
                    },
                    "timeout": {"type": "integer", "default": 60},
                    "memory": {"type": "string", "default": "2G"},
                    "smp": {"type": "string", "default": "4"},
                },
            },
        ),
    ]
