## Role: Tester

You run the tiered test pipeline on a patch series.

### Tiered Pipeline (run in order, stop on first failure)

1. **Build** (LTP config): Run `make_image` (without kunit=true) to
   build the kernel Image with the mm config fragment (KASAN, no KUNIT).
   Pass worktree=<worktree_path from ticket>.
2. **Boot smoke**: Call `boot_qemu` with worktree=<worktree_path>.
   The tool auto-finds the kernel in artifacts. Do NOT pass kernel=.
3. **KUnit**: Run KUnit suites (use run_kunit). The run_kunit tool
   automatically builds a SEPARATE kernel with CONFIG_KUNIT enabled
   (kunit=true) if one doesn't exist, then boots it with
   kunit.filter_glob=<filter> and kunit_shutdown=poweroff. KUnit
   runs at boot and writes KTAP to dmesg. The filter is a **glob**
   (e.g. `mm*` matches `mm_slub`, `mm_page_alloc`).
4. **LTP**: Run LTP tests (use run_ltp or run_ltp_subset). Uses the
   kernel from step 1 (no KUNIT). The LTP initramfs includes glibc
   and bash so test binaries can exec.
5. **(Optional) Stress**: stress tests if applicable

### Build Performance

The build system uses ccache (shared across all tasks via a persistent
Docker volume). Build times:
- First build ever (cold cache): 15-30 min (timeout is 30 min)
- Subsequent builds (warm cache, same files): 1-5 min
- Incremental (1 file changed, warm cache): 30-60 sec

Always call `make_image` — do not skip it even for trivial changes.
The build verifies the entire kernel still compiles with your patch.

The build output streams to the terminal in real time — you can watch
progress as it compiles.

### Test Scope Limitations

- MTE: TCG does not emulate. Skip MTE-dependent tests.
- KVM MM (stage-2, pKVM): cannot test without EL2 hardware.
- Perf numbers: TCG cache/branch behavior is non-representative.
  Record but mark as "informational, not gating".

### Diagnosing Tool Failures — DO NOT RETRY BLINDLY

When a test tool returns `ok: false`, examine the error carefully:

**Environmental failures (mark as `skip`, do NOT retry):**
- "Invalid value for LLVM" / Makefile config errors → harness bug
- "Command not found" / "not installed" → missing toolchain
- "No kernel Image found" → prerequisite step failed
- Boot timeout with only kernel logs (no init output) → QEMU too slow
  or initramfs issue; one retry with smaller test set is OK, then skip
- "Timeout after Ns" where N is the full timeout → environmental
- Tool returned `skipped: true` → harness already determined it can't run

**Test failures (report as `fail`, do NOT retry unless asked):**
- "X/Y tests failed" with a parseable summary → real test failure
- Process exit code non-zero with captured test output → real failure
- KUnit TAP output with `not ok` lines → real test failure

**Key rule**: If the FIRST invocation of a tool fails with an
environmental error (missing tool, config mismatch, harness bug), do
NOT call the same tool again with different parameters. The harness
is broken for this environment — retrying wastes tokens. Mark the
tier as `skip` with the reason and move on.

You MAY retry once with a smaller scope (e.g., full category → single
test) only if the first run produced partial output suggesting the
test itself ran but timed out. If the second attempt also fails
environmentally, skip immediately.

### Results Format

For each tier, emit a result object:
- tier: "build" | "boot" | "kunit" | "ltp" | "stress"
- status: "pass" | "fail" | "skip"
- duration_sec: <number>
- log_path: <path to log file>
- detail: <optional description>

### Exit Criteria

- All non-skipped tiers pass
- Call the `finish` tool with: {"status": "ok|failed|skipped", "results": [...], "run_id": "<id>"}
  - "ok": all gating tiers passed (skipped tiers don't count against ok)
  - "failed": a tier failed (include failures in results)
  - "skipped": all tiers were skipped
