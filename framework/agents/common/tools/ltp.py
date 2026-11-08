"""LTP test runner tools.

LTP tests run inside a QEMU-booted kernel using a custom initramfs
containing the full LTP test suite PLUS glibc (the test binaries are
dynamically linked, so they need ld-linux-aarch64.so.1 and libc.so).

The specific test category is selected based on the subsystem profile
(mm, sched, block, fs, etc.).

Uses 4GB RAM for QEMU to handle the LTP initramfs (~300MB with glibc).
Under TCG we use -cpu cortex-a72 (much faster than -cpu max).
"""
from __future__ import annotations
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd, run_cmd_streaming, WORKSPACE, find_kernel_image
import shutil
import os
import re
import uuid


def tools(subsystem) -> list[Tool]:
    """Create LTP tools."""

    def _find_ltp_dir() -> Path | None:
        """Find LTP installation directory.

        Checks for kirk (the LTP runner) or testcases/bin.
        """
        for path in ["/opt/ltp", "/usr/lib/ltp", "/usr/share/ltp"]:
            p = Path(path)
            if p.exists() and ((p / "kirk").exists() or (p / "testcases" / "bin").exists()):
                return p
        return None

    def _get_qemu_accel_args() -> list[str]:
        """Choose QEMU acceleration. Use cortex-a72 under TCG for speed."""
        accel = os.environ.get("KAG_QEMU_ACCEL", "tcg")
        if accel == "kvm":
            return ["-enable-kvm", "-cpu", "host"]
        else:
            return ["-accel", "tcg,thread=multi", "-cpu", "cortex-a72"]

    def _build_ltp_initramfs(ltp_dir: Path, base_initramfs: Path,
                              out_path: Path, init_script: str) -> dict:
        """Build an LTP initramfs with glibc + busybox + LTP suite.

        The base initramfs only has busybox-static (no libc). LTP test
        binaries are dynamically linked, so we need to add:
        - /lib/ld-linux-aarch64.so.1 (dynamic linker)
        - /lib/aarch64-linux-gnu/libc.so.6 (and other glibc libs)
        - /lib/aarch64-linux-gnu/libm, librt, libpthread, etc.
        - /bin/bash (kirk is a bash script)

        We copy these from the host (the tester container, which is
        Debian trixie on arm64, has glibc installed).
        """
        build_script = f"""
set -e
# Use /workspace for the LTP initramfs build — tmpfs is too small
# for the full LTP suite (~800MB) + cpio repacking.
WORK=$(mktemp -d -p /workspace)
cd $WORK

echo "[initramfs-build] Extracting base initramfs..." >&2
gzip -dc {base_initramfs} | cpio -idmv 2>&1 | tail -1

echo "[initramfs-build] Adding LTP suite..." >&2
cp -r {ltp_dir} ltp
# Strip debug symbols to reduce size
find ltp/testcases -type f -executable -exec strip --strip-debug {{}} \\; 2>/dev/null || true
find ltp/bin -type f -executable -exec strip --strip-debug {{}} \\; 2>/dev/null || true
find ltp/lib -type f -executable -exec strip --strip-debug {{}} \\; 2>/dev/null || true

echo "[initramfs-build] Adding glibc (required by LTP test binaries)..." >&2
# Copy the dynamic linker and glibc shared libs from the host.
# The tester container is Debian trixie on arm64.
mkdir -p lib/aarch64-linux-gnu lib64 usr/lib/aarch64-linux-gnu

# Dynamic linker (ld-linux-aarch64.so.1) — try multiple paths
LD_FOUND=""
for ld in /lib/ld-linux-aarch64.so.1 /lib/aarch64-linux-gnu/ld-linux-aarch64.so.1 \
          /usr/lib/ld-linux-aarch64.so.1 /usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1; do
  if [ -f "$ld" ]; then
    cp -L "$ld" lib/ld-linux-aarch64.so.1
    LD_FOUND="yes"
    echo "[initramfs-build] Found dynamic linker at: $ld" >&2
    break
  fi
done
if [ -z "$LD_FOUND" ]; then
  echo "[initramfs-build] ERROR: dynamic linker (ld-linux-aarch64.so.1) not found!" >&2
  echo "[initramfs-build] Searched: /lib, /lib/aarch64-linux-gnu, /usr/lib, /usr/lib/aarch64-linux-gnu" >&2
  # Don't exit — let the verification step catch it and report details
fi

# Core glibc libraries — copy ALL .so files from the host's lib dirs
# to ensure we don't miss any dependencies
echo "[initramfs-build] Copying glibc shared libs..." >&2
for dir in /lib/aarch64-linux-gnu /usr/lib/aarch64-linux-gnu; do
  if [ -d "$dir" ]; then
    for lib in libc.so.6 libm.so.6 librt.so.1 libpthread.so.0 libdl.so.2 \
               libutil.so.1 libcrypt.so.1 libresolv.so.2 libacl.so.1 \
               libattr.so.1 libcap.so.2 libaio.so.1 libpcre2-8.so.0 \
               libselinux.so.1; do
      if [ -f "$dir/$lib" ] && [ ! -f "lib/aarch64-linux-gnu/$lib" ]; then
        cp -L "$dir/$lib" lib/aarch64-linux-gnu/"$lib"
      fi
    done
  fi
done

echo "[initramfs-build] Adding bash (kirk is a bash script)..." >&2
# kirk uses bash-specific features. Copy bash from the host.
BASH_FOUND=""
for bp in /bin/bash /usr/bin/bash /usr/local/bin/bash; do
  if [ -x "$bp" ]; then
    cp -L "$bp" bin/bash
    BASH_FOUND="yes"
    echo "[initramfs-build] Found bash at: $bp" >&2
    break
  fi
done
if [ -z "$BASH_FOUND" ]; then
  echo "[initramfs-build] ERROR: bash not found!" >&2
fi

# Verify bash dependencies are satisfied (ldd shows what it needs)
if [ -f bin/bash ]; then
  echo "[initramfs-build] bash ldd output:" >&2
  ldd bin/bash 2>&1 | head -10 >&2
fi

# Add other utilities that runltp/LTP scripts need
for util in awk sed grep sort tr cut expr printf tee cat; do
  if [ -f /usr/bin/$util ] && [ ! -L bin/$util ]; then
    # Don't overwrite busybox symlinks for simple tools
    :
  fi
done
# Ensure awk exists (kirk uses it). busybox awk should suffice.

echo "[initramfs-build] Adding Python3 (kirk is a Python3 script)..." >&2
# Copy python3 binary and its shared libraries.
# kirk requires Python3 to run.
PYTHON3_BIN=""
for p in /usr/bin/python3 /usr/bin/python3.11 /usr/bin/python3.12; do
  if [ -x "$p" ]; then
    PYTHON3_BIN="$p"
    break
  fi
done
if [ -n "$PYTHON3_BIN" ]; then
  # Copy python3 binary to bin/ (no symlinks — symlinks create
  # circular references like bin/python3 -> python3)
  mkdir -p bin
  cp -L "$PYTHON3_BIN" bin/python3 2>/dev/null || true
  chmod +x bin/python3 2>/dev/null || true

  # Copy Python3 shared libraries
  PYTHON3_LIBS=$(ldd "$PYTHON3_BIN" 2>/dev/null | grep '=> /' | awk '{{print $3}}')
  for lib in $PYTHON3_LIBS; do
    if [ -f "$lib" ]; then
      dest_dir="lib/aarch64-linux-gnu"
      libdir=$(dirname "$lib" | xargs basename)
      if [ "$libdir" = "aarch64-linux-gnu" ]; then
        cp -L "$lib" "lib/aarch64-linux-gnu/$(basename $lib)" 2>/dev/null || true
      else
        cp -L "$lib" "lib/$(basename $lib)" 2>/dev/null || true
      fi
    fi
  done

  # Copy Python3 stdlib — kirk needs many modules.
  # Copy the full stdlib (it is ~30MB, acceptable for the initramfs).
  PY_VER=$(python3 -c 'import sys; print(f"python{{sys.version_info.major}}.{{sys.version_info.minor}}")' 2>/dev/null)
  if [ -n "$PY_VER" ]; then
    for pydir in /usr/lib/$PY_VER /usr/local/lib/$PY_VER; do
      if [ -d "$pydir" ]; then
        mkdir -p "usr/lib/$PY_VER"
        cp -a "$pydir/." "usr/lib/$PY_VER/" 2>/dev/null || true
        # Remove __pycache__ and tests to save space
        find "usr/lib/$PY_VER" -type d -name __pycache__ -exec rm -rf {{}} + 2>/dev/null || true
        find "usr/lib/$PY_VER" -type d -name test -exec rm -rf {{}} + 2>/dev/null || true
        find "usr/lib/$PY_VER" -type d -name tests -exec rm -rf {{}} + 2>/dev/null || true
        echo "[initramfs-build] Python3 stdlib copied ($PY_VER)" >&2
        break
      fi
    done
  fi
  echo "[initramfs-build] Python3 added" >&2
  ldd bin/python3 2>&1 | head -5 >&2
else
  echo "[initramfs-build] WARNING: python3 not found — kirk will not work!" >&2
fi

echo "[initramfs-build] Replacing init script..." >&2
cat > init << 'INITEOF'
{init_script}
INITEOF
chmod +x init

echo "[initramfs-build] Repacking initramfs (gzip -9)..." >&2
find . | cpio -o -H newc 2>/dev/null | gzip -9 > {out_path}

# Report size
SIZE=$(du -sh {out_path} | cut -f1)
echo "[initramfs-build] Done. Size: $SIZE" >&2

cd /
rm -rf $WORK
"""
        r = run_cmd(["bash", "-c", build_script], timeout=300)
        return r

    def run_ltp(category: str = None, tests: list[str] = None,
                worktree: str = None, timeout: int = 1200) -> dict:
        """Run LTP tests inside a QEMU-booted kernel.

        Creates a custom initramfs containing the full LTP test suite
        PLUS glibc and bash (the base initramfs only has busybox-static,
        but LTP test binaries are dynamically linked and kirk is a
        bash script). Boots the patched kernel and runs the specified
        test category.

        The init script writes delimiters around the LTP summary so we
        can parse pass/fail counts reliably from the retained stdout.
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

        # Determine kernel path — use shared helper that searches
        # artifacts dir first, then worktree, then build volume
        kernel = None
        if worktree:
            found = find_kernel_image(worktree)
            if found:
                kernel = str(found)
        if not kernel:
            found = find_kernel_image(None)
            if found:
                kernel = str(found)
        if not kernel:
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

        run_id = str(uuid.uuid4())[:8]
        # Per-task test artifacts: artifacts/<task-id>/test/
        task_id = Path(worktree).name if worktree else run_id
        role = os.environ.get("AGENT_ROLE", "unknown")
        out_dir = WORKSPACE / "artifacts" / task_id / role / "test"
        out_dir.mkdir(parents=True, exist_ok=True)

        # The init script for the LTP run.
        #
        # KEY DESIGN POINTS:
        # 1. Print "=== LTP TEST RUN ===" IMMEDIATELY after mounts, before
        #    anything else. If we don't see this marker, init itself failed
        #    (kernel panic, busybox missing, etc.) — distinct from LTP failing.
        # 2. Use /bin/busybox sh for the init script (shebang requirement).
        # 3. Use kirk (the new LTP runner) instead of runltp (deprecated/removed).
        #    kirk usage: kirk -f <suite> [-s <test1,test2,...>]
        #    Test suites are defined in /ltp/runtest/<suite>.
        # 4. Print delimiters around the LTP results so we can parse
        #    the summary from the last 8KB of stdout reliably.
        # 5. On any error, print a clear error marker and poweroff.
        # kirk is a Python3 script. The initramfs must include python3
        # for kirk to run inside QEMU.
        test_cmd = f"/bin/python3 /ltp/kirk -f {category}"
        if tests:
            test_cmd += f" -s {','.join(tests)}"

        init_script = f"""#!/bin/busybox sh
# Print marker IMMEDIATELY so we know init ran.
# NOTE: shebang MUST be #!/bin/busybox sh, NOT #!/bin/sh.
# The base initramfs only has /bin/busybox — /bin/sh symlink is
# created by 'busybox --install' below, but the kernel execs /init
# BEFORE that runs, so #!/bin/sh would fail with ENOENT.
echo "=== LTP INIT START ==="

/bin/busybox --install -s /bin 2>/dev/null
mount -t proc none /proc 2>/dev/null
mount -t sysfs none /sys 2>/dev/null
mount -t devtmpfs none /dev 2>/dev/null
mount -t tmpfs none /tmp 2>/dev/null

echo "=== LTP TEST RUN ==="
echo "Category: {category}"
echo "Tests: {tests or 'all in category'}"
echo "LTP dir: /ltp"
echo "bash: $([ -x /bin/bash ] && echo present || echo MISSING)"
echo "ld-linux: $([ -f /lib/ld-linux-aarch64.so.1 ] && echo present || echo MISSING)"
echo "libc: $([ -f /lib/aarch64-linux-gnu/libc.so.6 ] && echo present || echo MISSING)"
echo "runtest dir: $([ -d /ltp/runtest ] && echo present || echo MISSING)"
echo "testcases/bin: $([ -d /ltp/testcases/bin ] && echo present || echo MISSING)"
echo "python3: $([ -x /bin/python3 ] && echo present || echo MISSING)"
echo "kirk: $([ -f /ltp/kirk ] && echo present || echo MISSING)"
echo "---"

if [ ! -x /bin/bash ]; then
  echo "LTP_INIT_ERROR: bash not found in initramfs"
  echo "=== LTP END ==="
  poweroff -f
fi

if [ ! -f /ltp/kirk ]; then
  echo "LTP_INIT_ERROR: kirk not found at /ltp/kirk"
  echo "=== LTP END ==="
  poweroff -f
fi

if ! /bin/python3 --version 2>/dev/null; then
  echo "LTP_INIT_ERROR: python3 not working in initramfs"
  echo "=== LTP END ==="
  poweroff -f
fi

cd /ltp
export LTPROOT=/ltp
export PATH="/ltp/testcases/bin:$PATH"
# Set PYTHONHOME so python3 finds its stdlib in the initramfs
export PYTHONHOME=/usr
{test_cmd} > /tmp/ltp_output.log 2>&1
LTP_EXIT=$?

echo "---"
echo "LTP_EXIT_CODE=$LTP_EXIT"
echo "=== LTP STDOUT TAIL (last 100 lines) ==="
tail -n 100 /tmp/ltp_output.log 2>/dev/null
echo "=== LTP RESULTS FILE ==="
# kirk writes results to /ltp/results/ or outputs JSON to stdout
RESULTS_FILE=$(ls -t /ltp/results/LTP_RUN-*.log /ltp/results/*.log 2>/dev/null | head -1)
if [ -n "$RESULTS_FILE" ]; then
  cat "$RESULTS_FILE"
else
  echo "(no results file found — searching output for summary)"
  grep -E "Total Tests|Total Failures|Total Testcases|Test Failures|PASS|FAIL|SKIP" /tmp/ltp_output.log 2>/dev/null | tail -30 || true
fi
echo "=== LTP END ==="

sync
poweroff -f
"""

        # Build the LTP initramfs (with glibc + bash)
        ltp_initramfs_path = out_dir / "ltp_initramfs.cpio.gz"
        build_result = _build_ltp_initramfs(
            ltp_dir, base_initramfs, ltp_initramfs_path, init_script
        )
        if not build_result["ok"]:
            return {
                "ok": False,
                "error": f"Failed to build LTP initramfs: {build_result.get('stderr', '')[:500]}",
            }

        # ── Verify the initramfs contains critical files ───────────
        # Before booting QEMU (which takes 60+ seconds), verify the
        # initramfs has /bin/busybox, /bin/bash, /init, and the dynamic
        # linker. If any are missing, we'd waste a boot cycle discovering it.
        verify_script = f"""
set -e
TMP=$(mktemp -d -p /workspace)
cd $TMP
gzip -dc {ltp_initramfs_path} | cpio -idm --quiet 2>/dev/null

ERRORS=""

# Check /init exists and is executable
if [ ! -x init ]; then
  ERRORS="$ERRORS  /init missing or not executable"
fi

# Check /bin/busybox (needed for the shebang and basic commands)
if [ ! -x bin/busybox ]; then
  ERRORS="$ERRORS  /bin/busybox missing or not executable"
fi

# Check /bin/bash (runltp requires bash)
if [ ! -x bin/bash ]; then
  ERRORS="$ERRORS  /bin/bash missing or not executable"
fi

# Check dynamic linker (needed by bash and LTP test binaries)
if [ ! -f lib/ld-linux-aarch64.so.1 ]; then
  ERRORS="$ERRORS  /lib/ld-linux-aarch64.so.1 missing (bash/LTP won't exec)"
fi

# Check libc (needed by all dynamically linked binaries)
if [ ! -f lib/aarch64-linux-gnu/libc.so.6 ]; then
  ERRORS="$ERRORS  /lib/aarch64-linux-gnu/libc.so.6 missing"
fi

# Check kirk exists
if [ ! -f ltp/kirk ]; then
  ERRORS="$ERRORS  /ltp/kirk missing — LTP runner not installed"
fi

# Check python3 (kirk requires it)
if [ ! -x bin/python3 ]; then
  ERRORS="$ERRORS  python3 missing — kirk requires Python3"
fi

if [ -n "$ERRORS" ]; then
  echo "INITRAMFS_VERIFICATION_FAILED"
  echo "$ERRORS"
  # Also list what IS in the root
  echo "--- Root dir contents: ---"
  ls -la
  echo "--- bin/ contents: ---"
  ls -la bin/ 2>/dev/null | head -20
  echo "--- lib/ contents: ---"
  ls -la lib/ 2>/dev/null | head -20
  echo "--- lib/aarch64-linux-gnu/ contents: ---"
  ls -la lib/aarch64-linux-gnu/ 2>/dev/null | head -20
  exit 1
fi

echo "INITRAMFS_VERIFICATION_PASSED"
echo "init: $(file init 2>/dev/null | head -1)"
echo "busybox: $(file bin/busybox 2>/dev/null | head -1)"
echo "bash: $(file bin/bash 2>/dev/null | head -1)"
echo "ld-linux: $(file lib/ld-linux-aarch64.so.1 2>/dev/null | head -1)"
echo "kirk: $(file ltp/kirk 2>/dev/null | head -1)"
echo "python3: $(file bin/python3 2>/dev/null | head -1)"

cd /
rm -rf $TMP
"""
        verify_r = run_cmd(["bash", "-c", verify_script], timeout=60)
        if not verify_r["ok"]:
            return {
                "ok": False,
                "error": (
                    "LTP initramfs verification failed — critical files missing. "
                    f"This means the initramfs build step didn't copy all "
                    f"required files from the host. "
                    f"stdout: {verify_r.get('stdout', '')[:2000]}"
                ),
                "run_id": run_id,
                "category": category,
            }

        # Boot the kernel with the LTP initramfs.
        # 4GB RAM for stripped LTP (~200MB) + glibc + headroom.
        qemu_cmd = [
            "qemu-system-aarch64",
            "-machine", "virt",
            "-nodefaults",
            *_get_qemu_accel_args(),
            "-smp", "2", "-m", "4G",
            "-kernel", kernel,
            "-initrd", str(ltp_initramfs_path),
            "-append", "console=ttyAMA0 rdinit=/init",
            "-nographic", "-no-reboot",
            "-serial", "mon:stdio",
        ]

        log_path = str(out_dir / "ltp.log")
        r = run_cmd_streaming(
            qemu_cmd, timeout=timeout, prefix="    ",
            log_file=log_path,
        )

        stdout = r.get("stdout", "")

        # Check if init ran at all (our marker)
        init_ran = "=== LTP INIT START ===" in stdout

        if not init_ran:
            # Init never executed — kernel panic, OOM, or initramfs unpack failure
            return {
                "ok": False,
                "error": (
                    "LTP init script never ran (no INIT START marker). "
                    "Likely causes: kernel panic, initramfs too large, "
                    "or OOM during initramfs unpack. "
                    f"stdout tail: {stdout[-1500:]}"
                ),
                "run_id": run_id,
                "category": category,
                "log_path": log_path,
                "stdout": stdout[-4000:],
                "duration": r.get("duration", 0),
            }

        # Init ran. Parse the delimited LTP summary section.
        results_section = _extract_section(
            stdout, "=== LTP RESULTS FILE ===", "=== LTP END ==="
        )

        # Extract LTP_EXIT_CODE
        ltp_exit_match = re.search(r"LTP_EXIT_CODE=(\d+)", stdout)
        ltp_exit_code = int(ltp_exit_match.group(1)) if ltp_exit_match else None

        # Check for our init error marker
        if "LTP_INIT_ERROR:" in stdout:
            error_match = re.search(r"LTP_INIT_ERROR: (.*)", stdout)
            error_msg = error_match.group(1) if error_match else "LTP init error"
            return {
                "ok": False,
                "error": f"LTP init error: {error_msg}",
                "run_id": run_id,
                "category": category,
                "log_path": log_path,
                "stdout": stdout[-4000:],
                "duration": r.get("duration", 0),
            }

        # Parse summary
        total_tests, total_failures = _parse_ltp_summary(results_section or stdout)

        ran_successfully = (
            ltp_exit_code is not None
            and "=== LTP END ===" in stdout
        )

        if not ran_successfully:
            return {
                "ok": False,
                "error": (
                    "LTP init ran but did not complete (no LTP_END marker). "
                    "Likely a timeout or crash during test execution. "
                    f"stdout tail: {stdout[-1500:]}"
                ),
                "run_id": run_id,
                "category": category,
                "log_path": log_path,
                "stdout": stdout[-4000:],
                "duration": r.get("duration", 0),
            }

        all_passed = ltp_exit_code == 0 and total_failures == 0

        return {
            "ok": all_passed,
            "run_id": run_id,
            "category": category,
            "log_path": log_path,
            "stdout": stdout[-4000:],
            "results_section": results_section,
            "total_tests": total_tests,
            "total_failures": total_failures,
            "total_passed": total_tests - total_failures if total_tests else 0,
            "ltp_exit_code": ltp_exit_code,
            "duration": r.get("duration", 0),
        }

    def run_ltp_subset(tests: list[str], worktree: str = None,
                       timeout: int = 900) -> dict:
        """Run a specific subset of LTP tests by name."""
        return run_ltp(tests=tests, worktree=worktree, timeout=timeout)

    return [
        Tool(
            name="run_ltp",
            description=(
                "Run LTP tests inside a QEMU-booted kernel. "
                "Builds a full LTP initramfs (with glibc + bash so test "
                "binaries can exec) and boots the patched kernel. "
                "The test category is auto-selected from the subsystem "
                "profile (mm→mm, sched→sched, block→block, fs→fs). "
                "Pass worktree=<path from ticket> to find the kernel. "
                "For faster iteration, use run_ltp_subset with a small "
                "test list."
            ),
            fn=run_ltp,
            schema={
                "type": "object",
                "properties": {
                    "category": {"type": "string",
                                 "description": "LTP test category (auto-selected if omitted)"},
                    "tests": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "worktree": {"type": "string",
                                 "description": "Worktree path (from ticket) — used to find kernel Image"},
                    "timeout": {"type": "integer", "default": 1200},
                },
            },
        ),
        Tool(
            name="run_ltp_subset",
            description=(
                "Run a specific subset of LTP tests by name inside QEMU. "
                "Faster than run_ltp for inner-loop iteration — pass a "
                "small list of test names like ['mmap01', 'brk01']."
            ),
            fn=run_ltp_subset,
            schema={
                "type": "object",
                "properties": {
                    "tests": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "worktree": {"type": "string"},
                    "timeout": {"type": "integer", "default": 900},
                },
                "required": ["tests"],
            },
        ),
    ]


def _extract_section(text: str, start_marker: str, end_marker: str) -> str:
    """Extract text between two markers (inclusive of start, exclusive of end)."""
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return ""
    end_idx = text.find(end_marker, start_idx)
    if end_idx == -1:
        return text[start_idx:]
    return text[start_idx:end_idx]


def _parse_ltp_summary(text: str) -> tuple[int, int]:
    """Parse LTP summary for total tests and total failures.

    LTP runltp writes its summary in various formats depending on version:
        Total Tests: <N>
        Total Failures: <F>
    or:
        Total Testcases: <N>
        Total Failures: <F>
    or:
        Testcases Executed  N
        Test Failures      F
    """
    total_tests = 0
    total_failures = 0

    for pattern in [
        r"Total\s+Tests\s*:\s*(\d+)",
        r"Total\s+Testcases\s*:\s*(\d+)",
        r"Testcases\s+Executed\s+(\d+)",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            total_tests = int(m.group(1))
            break

    for pattern in [
        r"Total\s+Failures\s*:\s*(\d+)",
        r"Test\s+Failures\s+(\d+)",
        r"Failures\s*:\s*(\d+)",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            total_failures = int(m.group(1))
            break

    return total_tests, total_failures
