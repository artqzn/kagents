"""KUnit test runner tools.

Two execution modes:

1. **Boot mode** (default, recommended): Boot the pre-built kernel
   (already built by make_image) with `kunit.enable=1` and
   `kunit.filter_glob=<filter>` on the kernel command line. KUnit runs
   at boot and writes KTAP to dmesg. We capture dmesg via the serial
   console and parse the KTAP output.

   This mode does NOT use kunit.py and does NOT need a clean source
   tree. It reuses the kernel Image that the tester already built,
   which is the same kernel the LTP boot test uses.

2. **kunit.py mode** (fallback): If the kernel wasn't pre-built, fall
   back to kunit.py. This requires a clean source tree (no in-tree
   build artifacts) because kunit.py uses out-of-tree builds (O=) and
   the kernel Makefile refuses O= when the source tree is dirty.
   We attempt `make clean` first (preserves .config) but if that
   fails or the kernel Image is missing, we return skip.
"""
from __future__ import annotations
import os
import re
import uuid
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd, run_cmd_streaming, WORKSPACE, find_kernel_image, find_kernel_config


def tools(subsystem) -> list[Tool]:
    """Create KUnit tools."""

    def _detect_qemu_accel_args() -> list[str]:
        """Choose QEMU acceleration. cortex-a72 under TCG for speed."""
        accel = os.environ.get("KAG_QEMU_ACCEL", "tcg")
        if accel == "kvm":
            return ["-enable-kvm", "-cpu", "host"]
        else:
            return ["-accel", "tcg,thread=multi", "-cpu", "cortex-a72"]

    def _find_kernel(worktree: str = None) -> str | None:
        """Find the kernel Image using the shared helper.

        Searches artifacts dir first (out-of-tree build output), then
        worktree, then build volume.
        """
        found = find_kernel_image(worktree)
        return str(found) if found else None

    def _parse_ktap(text: str) -> dict:
        """Parse KTAP output from kernel dmesg.

        KTAP v1 format:
            KTAP version 1
            1..N
            ok 1 suite_name
                KTAP version 1
                1..M
                ok 1 test_name
                not ok 2 test_name
            ok 2 suite_name

        We do a simplified parse: count `ok N` and `not ok N` lines at
        any indentation level, excluding the `1..N` plan lines.

        Dmesg timestamp prefixes like "[    0.100000] " are stripped
        before matching.
        """
        results = []
        for line in text.split("\n"):
            line = line.rstrip()
            # Strip dmesg timestamp prefix like "[    0.100000] "
            line = re.sub(r"^\[\s*\d+\.\d+\]\s*", "", line)
            # Match "ok N" or "not ok N" (with optional leading whitespace)
            m = re.match(r"^\s*(not ok|ok)\s+(\d+)\s+(.*)$", line)
            if m:
                status_word, num, name = m.groups()
                results.append({
                    "num": num,
                    "name": name.strip(),
                    "status": "fail" if status_word == "not ok" else "pass",
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

    def _verify_kunit_config(worktree: str) -> dict | None:
        """Verify the built kernel has CONFIG_KUNIT=y.

        Checks the .config in the artifacts directory (preferred — it's
        the resolved config from the out-of-tree build) or the worktree.
        Returns None if OK, or an error dict if KUnit is not enabled.
        """
        config_path = find_kernel_config(worktree)
        if config_path is None:
            return {
                "ok": False,
                "error": (
                    "No .config found. Run make_image first (it applies "
                    "the subsystem config fragment which enables "
                    "CONFIG_KUNIT, then copies .config to the artifacts "
                    "directory)."
                ),
            }
        config_text = config_path.read_text()
        if "CONFIG_KUNIT=y" not in config_text:
            return {
                "ok": False,
                "error": (
                    f"CONFIG_KUNIT is not enabled in {config_path}. "
                    "The subsystem config fragment should set it — "
                    "check that make_image applied the fragment. "
                    "Look for CONFIG_KUNIT in the .config."
                ),
            }
        return None

    def run_kunit(worktree: str = None, filter: str = None,
                  timeout: int = 600) -> dict:
        """Run KUnit tests by booting the pre-built kernel.

        The kernel must have CONFIG_KUNIT=y and CONFIG_KUNIT_ALL_TESTS=y
        (the mm profile enables both). KUnit runs at boot and writes
        KTAP to dmesg, which we capture via the serial console.

        Args:
            worktree: Path to the worktree containing the built kernel.
            filter: KUnit suite glob filter (e.g. "mm*"). Defaults to
                    the subsystem's kunit_filter. The filter is
                    automatically suffixed with '*' if it doesn't
                    already contain a glob character, because
                    kunit.filter_glob uses glob matching and a bare
                    'mm' only matches a suite literally named 'mm',
                    not 'mm_slub' or 'mm_page_alloc'.
            timeout: QEMU boot timeout in seconds.
        """
        if filter is None:
            filter = getattr(subsystem, "kunit_filter", "")

        if not filter:
            return {
                "ok": True,
                "skipped": True,
                "reason": "No KUnit filter for this subsystem",
            }

        # kunit.filter_glob uses glob matching. A bare 'mm' only matches
        # a suite literally named 'mm'. Suite names use underscores
        # (e.g. 'mm_slub', 'mm_page_alloc'). Auto-append '*' if the
        # filter has no glob characters.
        if not any(c in filter for c in "*?["):
            filter = filter + "*"

        # Verify CONFIG_KUNIT is enabled before bothering to boot
        config_err = _verify_kunit_config(worktree)
        if config_err:
            return config_err

        kernel = _find_kernel(worktree)
        if not kernel:
            return {
                "ok": False,
                "error": (
                    "No kernel Image found. Run make_image first. "
                    "KUnit boot mode requires a pre-built kernel."
                ),
            }

        initrd = "/workspace/initramfs/initramfs.cpio.gz"
        if not Path(initrd).exists():
            return {
                "ok": False,
                "error": f"Base initramfs not found: {initrd}",
            }

        # Kernel cmdline:
        # - kunit.filter_glob=<filter>: only run matching suites (glob)
        # - kunit_shutdown=poweroff: power off after tests complete
        # - console=ttyAMA0: serial console for KTAP output
        # - rdinit=/init: run our init script (which just prints a marker
        #   and powers off — the kernel itself runs KUnit before init)
        #
        # NOTE: KUnit runs by default when CONFIG_KUNIT=y (via the
        # 'autorun' module parameter, default 1). There is no
        # kunit.enable parameter — don't add one.
        kernel_args = [
            "console=ttyAMA0",
            "earlycon=pl011,0x09000000",
            f"kunit.filter_glob={filter}",
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
        log_path = str(log_dir / "kunit.log")

        result = run_cmd_streaming(
            qemu_cmd, timeout=timeout, prefix="    ",
            log_file=log_path,
        )

        stdout = result.get("stdout", "")

        # Parse KTAP from the captured dmesg
        tap_summary = _parse_ktap(stdout)

        # Determine success:
        # - If we parsed at least one test result, use that.
        # - If we parsed zero results, KUnit didn't run — either the
        #   kernel doesn't have KUnit built in, or the filter matched
        #   nothing, or boot failed before KUnit ran.
        if tap_summary["total"] > 0:
            all_passed = tap_summary["failed"] == 0
            return {
                "ok": all_passed,
                "run_id": run_id,
                "filter": filter,
                "log_path": log_path,
                "stdout": stdout[-4000:],
                "tap": tap_summary,
                "total": tap_summary["total"],
                "passed": tap_summary["passed"],
                "failed": tap_summary["failed"],
                "duration": result.get("duration", 0),
                "error": None if all_passed else (
                    f"KUnit: {tap_summary['failed']}/{tap_summary['total']} tests failed"
                ),
            }

        # No KTAP output. Check if kernel even booted.
        boot_marker = "MM test kernel booted"
        if boot_marker not in stdout:
            return {
                "ok": False,
                "error": (
                    "Kernel did not boot (no init marker in output). "
                    f"stdout tail: {stdout[-1500:]}"
                ),
                "run_id": run_id,
                "log_path": log_path,
                "duration": result.get("duration", 0),
            }

        # Booted but no KUnit output — likely CONFIG_KUNIT not enabled
        # in the kernel config, or the filter matched no suites.
        # Check for KUnit-specific messages.
        if "kunit" in stdout.lower():
            return {
                "ok": False,
                "error": (
                    "Kernel booted but no KTAP output parsed. "
                    "Check that CONFIG_KUNIT=y and CONFIG_KUNIT_ALL_TESTS=y "
                    "are set in the kernel config, and that the filter "
                    f"'{filter}' matches at least one suite. "
                    f"stdout tail: {stdout[-2000:]}"
                ),
                "run_id": run_id,
                "log_path": log_path,
                "duration": result.get("duration", 0),
            }

        return {
            "ok": False,
            "error": (
                "Kernel booted but no KUnit/KTAP output found. "
                "CONFIG_KUNIT is likely not enabled. "
                f"stdout tail: {stdout[-2000:]}"
            ),
            "run_id": run_id,
            "log_path": log_path,
            "duration": result.get("duration", 0),
        }

    def parse_tap(tap_output: str) -> dict:
        """Parse TAP/KTAP output from KUnit or other test runners."""
        return _parse_ktap(tap_output)

    return [
        Tool(
            name="run_kunit",
            description=(
                "Run KUnit tests by booting the pre-built kernel with "
                "kunit.enable=1 and kunit.filter_glob=<subsystem_filter>. "
                "KUnit runs at boot and writes KTAP to dmesg, which we "
                "capture and parse. Requires make_image to have been run "
                "first (uses the same kernel Image). "
                "Pass worktree=<path from ticket> to find the kernel."
            ),
            fn=run_kunit,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string",
                                 "description": "Worktree path (from ticket) — used to find kernel Image"},
                    "filter": {"type": "string",
                               "description": "KUnit suite glob filter (default: subsystem filter, e.g. 'mm')"},
                    "timeout": {"type": "integer", "default": 600},
                },
            },
        ),
        Tool(
            name="parse_tap",
            description="Parse KTAP/TAP output from KUnit or other test runners.",
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
