"""LTP test runner tools.

LTP tests must run inside a booted kernel (inside QEMU), not directly
in the host container. This tool:
1. Adds a static LTP binary to a custom initramfs
2. Boots the kernel with that initramfs in QEMU
3. Runs the specified LTP test category
4. Returns the test output
"""
from __future__ import annotations
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd, WORKSPACE
import shutil
import os
import uuid


def tools(subsystem) -> list[Tool]:
    """Create LTP tools."""

    def _find_ltp_dir() -> Path | None:
        """Find LTP installation directory."""
        for path in ["/opt/ltp", "/usr/lib/ltp", "/usr/share/ltp"]:
            p = Path(path)
            if p.exists() and (p / "runltp").exists():
                return p
        runltp = shutil.which("runltp")
        if runltp:
            return Path(runltp).parent
        return None

    def run_ltp(category: str = None, tests: list[str] = None,
                worktree: str = None, timeout: int = 600) -> dict:
        """Run LTP tests inside a QEMU-booted kernel.

        Creates a custom initramfs containing the LTP test runner and
        the specified test category, boots the kernel, and captures
        the test output.
        """
        if category is None:
            categories = getattr(subsystem, "ltp_categories", [])
            if not categories:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "No LTP categories for this subsystem",
                }
            category = categories[0]

        ltp_dir = _find_ltp_dir()
        if ltp_dir is None:
            return {
                "ok": True,
                "skipped": True,
                "reason": "LTP not installed (build from source in Dockerfile)",
            }

        # Determine kernel path
        kernel = "/workspace/build/Image"
        wt = worktree or str(WORKSPACE / "src")
        wt_kernel = Path(wt) / "arch" / "arm64" / "boot" / "Image"
        if wt_kernel.exists():
            kernel = str(wt_kernel)
        elif not Path(kernel).exists():
            return {
                "ok": False,
                "error": "No kernel Image found. Run make_image first.",
            }

        # Find the base initramfs
        base_initramfs = Path("/workspace/initramfs/initramfs.cpio.gz")
        if not base_initramfs.exists():
            return {
                "ok": False,
                "error": "Base initramfs not found. Run build-initramfs.sh first.",
            }

        # Build a custom initramfs that includes LTP and runs it
        run_id = str(uuid.uuid4())[:8]
        out_dir = WORKSPACE / "artifacts" / "test-results" / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        # The init script for the LTP run
        test_cmd = f"runltp -f {category}"
        if tests:
            test_cmd += f" -s {' '.join(tests)}"

        init_script = f"""#!/bin/sh
/bin/busybox --install -s /bin
mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev

echo "=== LTP TEST RUN ==="
echo "Category: {category}"
echo "Tests: {tests or 'all in category'}"
echo "LTP dir: /ltp"
echo "---"

cd /ltp
{test_cmd} > /tmp/ltp_output.log 2>&1
LTP_EXIT=$?

echo "---"
echo "LTP_EXIT_CODE=$LTP_EXIT"
cat /tmp/ltp_output.log

poweroff -f
"""

        # Create the initramfs with LTP
        # We extract the base initramfs, add LTP, and repack
        build_script = f"""
set -e
WORK=$(mktemp -d)
cd $WORK

# Extract base initramfs
gzip -dc {base_initramfs} | cpio -idmv

# Add LTP test suite
cp -r {ltp_dir} ltp

# Replace init script
cat > init << 'INITEOF'
{init_script}
INITEOF
chmod +x init

# Repack
find . | cpio -o -H newc | gzip > {out_dir}/ltp_initramfs.cpio.gz

# Cleanup
cd /
rm -rf $WORK
"""

        r = run_cmd(["bash", "-c", build_script], timeout=120)
        if not r["ok"]:
            return {
                "ok": False,
                "error": f"Failed to build LTP initramfs: {r.get('stderr', '')[:500]}",
            }

        # Boot the kernel with the LTP initramfs
        qemu_accel = os.environ.get("KAG_QEMU_ACCEL", "tcg")
        if qemu_accel == "kvm":
            qemu_args = ["-enable-kvm", "-cpu", "host"]
        else:
            qemu_args = ["-accel", "tcg,thread=multi", "-cpu", "max"]

        qemu_cmd = [
            "qemu-system-aarch64",
            "-machine", "virt",
            "-nodefaults",  # Don't create default devices (avoids efi-virtio.rom)
            *qemu_args,
            "-smp", "2", "-m", "1G",
            "-kernel", kernel,
            "-initrd", str(out_dir / "ltp_initramfs.cpio.gz"),
            "-append", "console=ttyAMA0 rdinit=/init",
            "-nographic", "-no-reboot",
            "-serial", "mon:stdio",
        ]

        r = run_cmd(qemu_cmd, timeout=timeout)

        # Save boot log
        log_file = out_dir / "ltp.log"
        log_file.write_text(r.get("stdout", ""))

        # Parse output for pass/fail
        stdout = r.get("stdout", "")
        ltp_exit_match = "LTP_EXIT_CODE=0" in stdout

        return {
            "ok": ltp_exit_match,
            "run_id": run_id,
            "category": category,
            "log_path": str(log_file),
            "stdout": stdout[-4000:],
        }

    def run_ltp_subset(tests: list[str], worktree: str = None,
                       timeout: int = 600) -> dict:
        """Run a specific subset of LTP tests by name."""
        return run_ltp(tests=tests, worktree=worktree, timeout=timeout)

    return [
        Tool(
            name="run_ltp",
            description=(
                "Run LTP tests inside a QEMU-booted kernel. "
                "Creates a custom initramfs with LTP, boots the kernel, "
                "and runs the specified test category. "
                "Uses subsystem's ltp_categories if no explicit category given."
            ),
            fn=run_ltp,
            schema={
                "type": "object",
                "properties": {
                    "category": {"type": "string",
                                 "description": "LTP test category (e.g. 'mm', 'sched')"},
                    "tests": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "worktree": {"type": "string"},
                    "timeout": {"type": "integer", "default": 600},
                },
            },
        ),
        Tool(
            name="run_ltp_subset",
            description="Run a specific subset of LTP tests by name inside QEMU.",
            fn=run_ltp_subset,
            schema={
                "type": "object",
                "properties": {
                    "tests": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "worktree": {"type": "string"},
                    "timeout": {"type": "integer", "default": 600},
                },
                "required": ["tests"],
            },
        ),
    ]
