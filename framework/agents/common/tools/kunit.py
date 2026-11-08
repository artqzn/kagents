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
import sys
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

    def run_kunit(worktree: str = None, filter: str = None,
                  timeout: int = 600) -> dict:
        """Run KUnit tests by booting the pre-built kernel.

        If no kernel with CONFIG_KUNIT is found, automatically builds
        one using make_image(kunit=true) before booting.

        The filter is ALWAYS taken from the subsystem's kunit_filter
        config. The LLM-provided filter parameter is ignored because
        LLMs tend to use 'mm*' which matches zero suites (suite names
        don't start with 'mm').

        Args:
            worktree: Path to the worktree containing the built kernel.
            filter: Ignored — always uses subsystem's kunit_filter.
            timeout: QEMU boot timeout in seconds.
        """
        # Always use the subsystem's configured filter — ignore the
        # LLM-provided value. LLMs frequently try 'mm*' or 'mm' which
        # matches zero KUnit suites, wasting 15+ minutes on a QEMU
        # timeout. The subsystem profile has the correct filter (e.g.
        # 'slub*' for mm).
        filter = getattr(subsystem, "kunit_filter", "")

        if not filter:
            return {
                "ok": True,
                "skipped": True,
                "reason": "No KUnit filter for this subsystem",
            }

        # kunit.filter_glob uses glob matching.
        if not any(c in filter for c in "*?["):
            filter = filter + "*"

        # Check if we have a kernel with KUnit enabled.
        # If not, build one with kunit=true.
        config_path = find_kernel_config(worktree)
        need_kunit_build = True
        if config_path and config_path.exists():
            config_text = config_path.read_text()
            if "CONFIG_KUNIT=y" in config_text:
                need_kunit_build = False
                print(f"    ℹ  KUnit kernel already built: {config_path}",
                      file=sys.stderr, flush=True)

        if need_kunit_build:
            print(f"    ℹ  No KUnit kernel found — building with kunit=true...",
                  file=sys.stderr, flush=True)
            from framework.agents.common.tools.build import tools as build_tools
            # Find make_image in the build tools
            for t in build_tools(subsystem):
                if t.name == "make_image":
                    build_result = t.fn(worktree=worktree, kunit=True)
                    if not build_result.get("ok"):
                        return {
                            "ok": False,
                            "error": f"KUnit kernel build failed: {build_result.get('error', '')[:500]}",
                        }
                    print(f"    ℹ  KUnit kernel built successfully",
                          file=sys.stderr, flush=True)
                    break
            else:
                return {"ok": False, "error": "make_image tool not found"}

        # Find the kernel — find_kernel_image searches build-kunit/ first
        # for the current role, then build/
        kernel = _find_kernel(worktree)
        if not kernel:
            return {
                "ok": False,
                "error": (
                    "No kernel Image found after KUnit build. "
                    "Check artifacts directory."
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
        # Per-task test artifacts: artifacts/<task-id>/test/
        task_id = Path(worktree).name if worktree else run_id
        role = os.environ.get("AGENT_ROLE", "unknown")
        log_dir = WORKSPACE / "artifacts" / task_id / role / "test"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / "kunit.log")

        result = run_cmd_streaming(
            qemu_cmd, timeout=timeout, prefix="    ",
            log_file=log_path,
        )

        stdout = result.get("stdout", "")

        # Read the FULL output from the log file (stdout is truncated
        # to last 8000 chars — boot messages at the start get lost
        # when KUnit produces large output).
        full_output = ""
        try:
            full_output = Path(log_path).read_text()
        except Exception:
            full_output = stdout  # fallback to truncated stdout

        # Parse KTAP from the FULL output (not truncated stdout)
        tap_summary = _parse_ktap(full_output)

        # Determine success:
        # - If we parsed at least one test result, use that.
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

        # No KTAP output parsed. Check the FULL output for boot messages.
        boot_indicators = [
            "Linux version",          # kernel boot banner
            "Kernel command line",    # cmdline echo
            "Booting Linux on",       # arm64 boot message
        ]
        kernel_booted = any(ind in full_output for ind in boot_indicators)

        if not kernel_booted:
            return {
                "ok": False,
                "error": (
                    "Kernel did not boot (no boot messages in log). "
                    "Likely a kernel panic or QEMU failure. "
                    f"stdout tail: {stdout[-1500:]}"
                ),
                "run_id": run_id,
                "log_path": log_path,
                "duration": result.get("duration", 0),
            }

        # Kernel booted but no KTAP output. Could be:
        # - Filter matched no suites (kunit.filter_glob didn't match)
        # - CONFIG_KUNIT not enabled
        # Check for KUnit messages in the FULL output
        if "kunit" in full_output.lower() or "KTAP" in full_output:
            return {
                "ok": False,
                "error": (
                    "Kernel booted and KUnit is present, but no KTAP test "
                    "results were parsed. The filter '" + filter + "' may "
                    "not match any suite. Check the kunit log: " + log_path
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
                f"Log: {log_path}"
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
                "Run KUnit tests by booting a kernel with CONFIG_KUNIT "
                "enabled. If no KUnit kernel exists, automatically builds "
                "one with make_image(kunit=true). Uses the subsystem's "
                "configured kunit_filter (e.g. 'slub*' for mm) — the "
                "filter parameter is ignored. "
                "Pass worktree=<path from ticket>."
            ),
            fn=run_kunit,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string",
                                 "description": "Worktree path (from ticket) — used to find kernel Image"},
                    "filter": {"type": "string",
                               "description": "Ignored — uses subsystem's kunit_filter automatically"},
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
