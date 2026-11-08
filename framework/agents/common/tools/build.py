"""Build tools — out-of-tree kernel compilation on tmpfs.

Architecture:
    Source tree (worktree, bind-mounted from host)
        │
        │  make -C<source> O=/tmp/kbuild Image
        ▼
    Build dir (/tmp/kbuild, tmpfs = RAM-backed)
        │
        │  Copy Image, .config, System.map, vmlinux, etc.
        ▼
    Artifacts dir (/workspace/artifacts/<task-id>/<role>/build/, host-visible)

Benefits:
    - Build artifacts written to RAM (tmpfs), not through VirtioFS → 3-5× faster
    - Source tree stays clean → no kunit.py "source tree not clean" errors
    - Artifacts on host → all containers share the same build output
    - ccache persists across containers (named volume)
    - Requires 12g+ container memory so tmpfs doesn't compete with compiler
"""
from __future__ import annotations
import os
import sys
import platform
import hashlib
import shutil
import datetime
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd, run_cmd_streaming, WORKSPACE


def tools(subsystem) -> list[Tool]:
    """Create build tools."""

    def _resolve_worktree(worktree: str = None) -> str:
        """Resolve worktree path to an absolute path.

        Does NOT fall back to /workspace/src if the worktree doesn't
        exist — that would silently build against the wrong tree and
        potentially leave artifacts in the main source tree. Instead,
        returns the path as-is and lets make fail with a clear error.
        """
        if not worktree or worktree == "None":
            return str(WORKSPACE / "src")
        p = Path(worktree)
        if not p.is_absolute():
            p = WORKSPACE / p
        return str(p)

    def _get_build_dir(worktree: str, kunit: bool = False) -> str:
        """Get the out-of-tree build directory.

        macOS: /tmp/kbuild (tmpfs — faster than VirtioFS bind mount)
        Linux: /workspace/.kbuild (bind mount — native ext4, no RAM waste)

        On Linux, tmpfs counts against mem_limit. With 12g container
        and 8g tmpfs, the compiler only gets 4g → swapping → slow.
        The bind mount on Linux is already fast (native ext4).
        """
        import platform as _pf
        is_mac = _pf.system() == "Darwin"

        if kunit:
            build_dir = "/tmp/kbuild-kunit" if is_mac else "/workspace/.kbuild-kunit"
            other_dir = Path("/tmp/kbuild" if is_mac else "/workspace/.kbuild")
        else:
            build_dir = "/tmp/kbuild" if is_mac else "/workspace/.kbuild"
            other_dir = Path("/tmp/kbuild-kunit" if is_mac else "/workspace/.kbuild-kunit")

        # Free space: remove the other build dir (artifacts already saved)
        if other_dir.exists():
            shutil.rmtree(other_dir, ignore_errors=True)

        Path(build_dir).mkdir(parents=True, exist_ok=True)
        return build_dir

    def _get_source_dir(worktree: str) -> str:
        """Get a fixed-path source directory for building.

        Creates a symlink from /tmp/src → the actual worktree path.
        This is critical for ccache: the compiler's -I include paths
        contain the source dir. If different tasks have different
        source paths (e.g. /workspace/branches/.../aaa111 vs .../bbb222),
        ccache sees them as different compilations — no cache hits.

        By symlinking to a fixed /tmp/src path, all tasks build from
        the same logical path, so ccache hits across tasks.
        """
        cwd = _resolve_worktree(worktree)
        fixed_src = "/tmp/src"

        # Create symlink (remove old one first)
        if Path(fixed_src).is_symlink() or Path(fixed_src).exists():
            Path(fixed_src).unlink()
        Path(fixed_src).symlink_to(cwd)

        return fixed_src

    def _make_cmd(target: str, arch: str = None, jobs: int = None,
                  cross_compile: bool = None,
                  source_dir: str = None) -> list[str]:
        """Build a make command with ccache, proper arch and cross-compile.

        Uses -C <source_dir> so that make reads Makefiles from the
        source tree but writes all output (including host tools like
        fixdep) to the O= build directory.

        Also passes -ffile-prefix-map to normalize source paths for
        ccache. Without this, different task-ids in the worktree path
        (/workspace/branches/.../<task-id>/) cause different -I flags
        → different ccache keys → 66% miss rate. The flag remaps the
        actual source dir to /workspace/src so ccache always sees the
        same paths regardless of which task is building.

        Args:
            target: Make target (e.g. "Image", "defconfig", "olddefconfig")
            arch: Target architecture
            jobs: Number of parallel jobs
            cross_compile: Whether to add CROSS_COMPILE=
            source_dir: Source tree path for -C. If None, no -C is
                        added (caller must cd to the right dir).
        """
        arch = arch or os.environ.get("KAG_ARCH", "arm64")

        if cross_compile is None:
            host_arch = platform.machine()
            cross_compile = (
                host_arch == "x86_64" and arch == "arm64"
            )

        cmd = ["make"]
        if source_dir:
            cmd.append(f"-C{source_dir}")
        if cross_compile:
            cmd.append("CROSS_COMPILE=aarch64-linux-gnu-")
        cmd.append(f"ARCH={arch}")
        if os.environ.get("KAG_LLVM", "1") == "1":
            cmd.append("LLVM=1")
            # CC="ccache clang" is set via ENV in Dockerfile, but we
            # also pass it explicitly here for clarity and to ensure
            # it overrides any Makefile CC assignment.
            cmd.append('CC=ccache clang')

        # Normalize source paths for ccache via -ffile-prefix-map.
        # This remaps the actual source dir (which contains the task-id)
        # to /workspace/src, so ccache sees consistent -I flags across
        # all tasks. Without this, each task gets its own ccache keys
        # because the worktree path differs.
        #
        # We need the REAL source dir (not the /tmp/src symlink) because
        # make resolves symlinks and passes the real path to the compiler.
        # The real source dir is what _get_source_dir's symlink points to.
        if source_dir:
            # Resolve the symlink to get the real path
            real_source = str(Path(source_dir).resolve())
            # Only add the map if the real path differs from /workspace/src
            # (deploy sanity check builds from /workspace/src directly)
            if real_source != "/workspace/src":
                cmd.append(
                    f'KCFLAGS=-ffile-prefix-map={real_source}=/workspace/src'
                )

        if jobs is None:
            try:
                nproc_result = os.popen('nproc 2>/dev/null').read().strip()
                if nproc_result and nproc_result.isdigit():
                    jobs = int(nproc_result)
                else:
                    jobs = os.cpu_count() or 4
            except Exception:
                jobs = os.cpu_count() or 4

        cmd.append(f"-j{jobs}")
        cmd.append(target)
        return cmd

    def _apply_config_fragment(config_path: Path) -> dict | None:
        """Apply subsystem config fragment to a .config file.

        Merges the subsystem's config-fragment.config into the given
        .config path by overriding existing entries and appending new
        ones. Does NOT run olddefconfig — the caller must do that
        separately to resolve dependencies.

        Returns None on success, or an error dict on failure.
        """
        # Find the config fragment. SUBSYSTEM_DIR is a module-level
        # variable in the profile module, so we check both the module
        # and the subsystem object for it.
        config_fragment = None

        # Try module-level SUBSYSTEM_DIR
        sub_module = sys.modules.get(getattr(subsystem, "__module__", None), None) \
            if subsystem else None
        if sub_module and hasattr(sub_module, "SUBSYSTEM_DIR"):
            cf = Path(str(sub_module.SUBSYSTEM_DIR)) / "config-fragment.config"
            if cf.exists():
                config_fragment = cf

        # Try as attribute of subsystem object
        if not config_fragment:
            sub_dir = getattr(subsystem, "SUBSYSTEM_DIR", None) if subsystem else None
            if sub_dir:
                cf = Path(str(sub_dir)) / "config-fragment.config"
                if cf.exists():
                    config_fragment = cf

        # Try subsystems/<name>/ directory
        if not config_fragment and subsystem:
            sub_name = getattr(subsystem, "name", None) or getattr(subsystem, "__name__", None)
            if sub_name:
                from framework.agents.common.prompt import SUBSYSTEMS
                cf = SUBSYSTEMS / sub_name / "config-fragment.config"
                if cf.exists():
                    config_fragment = cf

        if not config_fragment:
            print(f"    ⚠  No config fragment found for subsystem",
                  file=sys.stderr, flush=True)
            return None

        print(f"    ℹ  Config fragment: {config_fragment}",
              file=sys.stderr, flush=True)
        return _apply_config_fragment_to_path(config_path, config_fragment)

    def _apply_config_fragment_to_path(config_path: Path, config_fragment: Path) -> dict | None:
        """Apply a specific config fragment file to a .config path.

        Returns None on success, or an error dict on failure.
        """
        if not config_path.exists():
            return {"ok": False, "error": f".config not found at {config_path}"}

        fragment_text = Path(str(config_fragment)).read_text()
        existing = config_path.read_text()

        # Parse fragment into a dict
        fragment_entries = {}
        for line in fragment_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                if line.startswith("# CONFIG_") and "is not set" in line:
                    cfg_name = line.split()[1]
                    fragment_entries[cfg_name] = None
                continue
            if "=" in line and line.startswith("CONFIG_"):
                parts = line.split("=", 1)
                cfg_name = parts[0].strip()
                cfg_val = parts[1].strip()
                fragment_entries[cfg_name] = cfg_val

        # Merge: override existing entries, append new ones
        new_lines = []
        seen_configs = set()
        for line in existing.splitlines():
            stripped = line.strip()
            if stripped.startswith("# CONFIG_") and "is not set" in stripped:
                cfg_name = stripped.split()[1]
                seen_configs.add(cfg_name)
                if cfg_name in fragment_entries:
                    if fragment_entries[cfg_name] is None:
                        new_lines.append(stripped)
                    else:
                        new_lines.append(f"{cfg_name}={fragment_entries[cfg_name]}")
                else:
                    new_lines.append(line)
            elif stripped.startswith("CONFIG_") and "=" in stripped:
                cfg_name = stripped.split("=")[0].strip()
                seen_configs.add(cfg_name)
                if cfg_name in fragment_entries:
                    if fragment_entries[cfg_name] is None:
                        new_lines.append(f"# {cfg_name} is not set")
                    else:
                        new_lines.append(f"{cfg_name}={fragment_entries[cfg_name]}")
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        for cfg_name, cfg_val in fragment_entries.items():
            if cfg_name not in seen_configs:
                if cfg_val is None:
                    new_lines.append(f"# {cfg_name} is not set")
                else:
                    new_lines.append(f"{cfg_name}={cfg_val}")

        config_path.write_text("\n".join(new_lines) + "\n")
        return None

    def _ccache_stats() -> dict:
        """Get ccache statistics."""
        r = run_cmd(["ccache", "-s"], timeout=10)
        raw_output = (r.get("stdout", "") + r.get("stderr", "")).strip()
        if not r["ok"] or not raw_output:
            return {"available": False, "error": r.get("stderr", "")[:200]}
        stats = {"available": True, "raw": raw_output[-1000:]}
        for line in raw_output.splitlines():
            stripped = line.strip()
            if "Hits:" in stripped:
                stats["hits"] = stripped
            elif "Misses:" in stripped:
                stats["misses"] = stripped
            elif "Cache size:" in stripped:
                stats["cache_size"] = stripped
        return stats

    def make_image(worktree: str = None, arch: str = None,
                   include_vmlinux: bool = False,
                   fast: bool = False,
                   kunit: bool = False) -> dict:
        """Build the kernel Image using out-of-tree build on tmpfs.

        Build artifacts are written to /tmp/kbuild (tmpfs, RAM-backed).
        After the build, the kernel Image and debug files are copied
        to /workspace/artifacts/<task-id>/<role>/build/.

        Args:
            worktree: Path to the git worktree (from ticket).
            arch: Target architecture (default: arm64).
            include_vmlinux: If True, copy vmlinux to artifacts.
            fast: Ignored (kept for backward compat).
            kunit: If True, also apply the subsystem kunit-fragment.config
                   on top of the base config. This enables CONFIG_KUNIT
                   and CONFIG_KUNIT_ALL_TESTS so KUnit runs at boot.

        Returns dict with:
            - ok: True/False
            - artifacts_dir: path to the artifacts directory on host
            - kernel_path: path to the kernel Image in artifacts
            - build_dir: path to the tmpfs build dir (ephemeral)
            - copied_files: list of files copied to artifacts
            - ccache_before/ccache_after: cache statistics
        """
        import uuid
        
        # Normalize booleans — LLMs sometimes pass "true"/"false" strings
        if isinstance(include_vmlinux, str):
            include_vmlinux = include_vmlinux.lower() in ("true", "1", "yes")
        if isinstance(fast, str):
            fast = fast.lower() in ("true", "1", "yes")
        if isinstance(kunit, str):
            kunit = kunit.lower() in ("true", "1", "yes")
        cwd = _resolve_worktree(worktree)
        source_dir = _get_source_dir(worktree)
        task_id = Path(cwd).name
        role = os.environ.get("AGENT_ROLE", "unknown")

        # Check for stale in-tree build artifacts (from v0.1.x in-tree
        # builds). The kernel Makefile refuses out-of-tree builds if
        # the source tree is not clean. Use 'make mrproper' — if it
        # fails (e.g. on a corrupted tree), fall back to 'git clean'.
        stale_markers = [
            Path(cwd) / "scripts" / "basic" / "fixdep",
            Path(cwd) / "vmlinux",
            Path(cwd) / "System.map",
            Path(cwd) / ".config",
        ]
        stale_found = [str(m.relative_to(cwd)) for m in stale_markers if m.exists()]
        if stale_found:
            print(f"    ⚠  Source tree has stale in-tree build artifacts: "
                  f"{', '.join(stale_found)}", file=sys.stderr, flush=True)
            print(f"    Running make mrproper...", file=sys.stderr, flush=True)

            cmd = _make_cmd("mrproper", arch=arch, source_dir=source_dir)
            r = run_cmd(cmd, timeout=120)
            if not r["ok"]:
                # mrproper failed (e.g. corrupted tree) — fall back to git clean
                print(f"    ⚠  make mrproper failed — falling back to git clean...",
                      file=sys.stderr, flush=True)
                r2 = run_cmd(["git", "-C", cwd, "clean", "-fdx"], timeout=120)
                if not r2["ok"]:
                    return {"ok": False, "error": f"Source cleanup failed: {r2.get('stderr', '')[:300]}"}
                run_cmd(["git", "-C", cwd, "checkout", "--", "."], timeout=60)
            print(f"    ✓ Source tree cleaned", file=sys.stderr, flush=True)

        # Out-of-tree build directory on tmpfs (RAM-backed)
        build_dir = _get_build_dir(worktree, kunit=kunit)

        # Log build dir and mode
        print(f"    ℹ  Build dir: {build_dir}", file=sys.stderr, flush=True)
        print(f"    ℹ  Source dir: {source_dir}", file=sys.stderr, flush=True)
        print(f"    ℹ  Config: defconfig + mm fragment", file=sys.stderr, flush=True)

        # ccache stats before build
        ccache_before = _ccache_stats()
        if ccache_before.get("available"):
            print(f"    ℹ  ccache before: {ccache_before.get('hits', '?')} | "
                  f"{ccache_before.get('misses', '?')} | "
                  f"{ccache_before.get('cache_size', '?')}",
                  file=sys.stderr, flush=True)

        # Prepare .config in the build directory
        build_config = Path(build_dir) / ".config"

        # Always (re-)apply the config fragment and run olddefconfig.
        # This ensures the subsystem config (CONFIG_KUNIT, etc.) is
        # always present, even on rebuilds. The cost is ~3-5s, negligible
        # compared to the 30+ second build time.
        #
        # We DO skip defconfig if a .config already exists (from a prior
        # make_image in the same session), but we always re-apply the
        # fragment + olddefconfig to ensure correctness.
        if not build_config.exists():
            wt_config = Path(cwd) / ".config"
            if wt_config.exists():
                shutil.copy(wt_config, build_config)
            else:
                # Run defconfig in the build dir.
                # Use -C <source> so host tools (fixdep) are built in
                # the O= build dir (tmpfs), NOT in the source tree
                # (bind mount — would get "Permission denied").
                cmd = _make_cmd("defconfig", arch=arch, source_dir=source_dir)
                cmd.append(f"O={build_dir}")
                r = run_cmd(cmd, timeout=60)  # cwd doesn't matter with -C
                if not r["ok"]:
                    return r

        # Apply subsystem config fragment (always — all builds use the
        # same base config so ccache hits across deploy/implementer/tester)
        err = _apply_config_fragment(build_config)
        if err:
            return err
        print(f"    ℹ  Applied subsystem config fragment",
              file=sys.stderr, flush=True)

        # If kunit=True, also apply the kunit-fragment.config
        if kunit:
            kunit_fragment = None
            sub_module = sys.modules.get(getattr(subsystem, "__module__", None), None) if subsystem else None
            if sub_module and hasattr(sub_module, "SUBSYSTEM_DIR"):
                kf = Path(str(sub_module.SUBSYSTEM_DIR)) / "kunit-fragment.config"
                if kf.exists():
                    kunit_fragment = kf
            if kunit_fragment:
                # Temporarily swap the config_fragment to apply kunit fragment
                err = _apply_config_fragment_to_path(build_config, kunit_fragment)
                if err:
                    return err
                print(f"    ℹ  Applied KUnit config fragment ({kunit_fragment})",
                      file=sys.stderr, flush=True)
            else:
                print(f"    ⚠  kunit=True but no kunit-fragment.config found",
                      file=sys.stderr, flush=True)

        # Log CONFIG_KUNIT state BEFORE olddefconfig
        pre_config = build_config.read_text()
        for cfg_name in ["CONFIG_KUNIT", "CONFIG_KUNIT_ALL_TESTS"]:
            for line in pre_config.splitlines():
                if cfg_name in line and not line.strip().startswith("#"):
                    print(f"    ℹ  Before olddefconfig: {line.strip()}",
                          file=sys.stderr, flush=True)
                    break
            else:
                # Check if it's explicitly disabled
                for line in pre_config.splitlines():
                    if f"# {cfg_name} is not set" in line:
                        print(f"    ℹ  Before olddefconfig: {line.strip()}",
                              file=sys.stderr, flush=True)
                        break

        # Run olddefconfig to resolve all dependencies
        cmd = _make_cmd("olddefconfig", arch=arch, source_dir=source_dir)
        cmd.append(f"O={build_dir}")
        r = run_cmd(cmd, timeout=60)
        if not r["ok"]:
            return {"ok": False, "error": f"olddefconfig failed: {r.get('stderr', '')[:300]}"}

        # Log CONFIG_KUNIT state AFTER olddefconfig
        post_config = build_config.read_text()
        for cfg_name in ["CONFIG_KUNIT", "CONFIG_KUNIT_ALL_TESTS"]:
            for line in post_config.splitlines():
                if cfg_name in line and not line.strip().startswith("#"):
                    print(f"    ℹ  After olddefconfig:  {line.strip()}",
                          file=sys.stderr, flush=True)
                    break
            else:
                for line in post_config.splitlines():
                    if f"# {cfg_name} is not set" in line:
                        print(f"    ℹ  After olddefconfig:  {line.strip()}",
                              file=sys.stderr, flush=True)
                        break

        # ── Verify critical config options survived olddefconfig ──
        # olddefconfig can turn off config options that have unmet
        # dependencies. If the subsystem fragment set CONFIG_KUNIT=y
        # but olddefconfig turned it off, print diagnostics so we can
        # understand why.
        final_config = build_config.read_text()
        critical_configs = []

        # Find config fragment via the same logic as _apply_config_fragment
        config_fragment = None
        sub_module = sys.modules.get(getattr(subsystem, "__module__", None), None) if subsystem else None
        if sub_module and hasattr(sub_module, "SUBSYSTEM_DIR"):
            cf = Path(str(sub_module.SUBSYSTEM_DIR)) / "config-fragment.config"
            if cf.exists():
                config_fragment = cf
        if not config_fragment:
            sub_dir = getattr(subsystem, "SUBSYSTEM_DIR", None) if subsystem else None
            if sub_dir:
                cf = Path(str(sub_dir)) / "config-fragment.config"
                if cf.exists():
                    config_fragment = cf

        if config_fragment:
            for line in config_fragment.read_text().splitlines():
                line = line.strip()
                if line.startswith("CONFIG_") and "=y" in line:
                    critical_configs.append(line.split("=")[0].strip())

        missing = []
        for cfg in critical_configs:
            if f"{cfg}=y" not in final_config:
                missing.append(cfg)

        if missing:
            
            print(f"    ⚠  Config options turned off by olddefconfig: "
                  f"{', '.join(missing)}", file=sys.stderr, flush=True)

            # Print diagnostic: show what olddefconfig did to each missing config
            for cfg in missing:
                for line in final_config.splitlines():
                    if cfg in line:
                        print(f"    ⚠  {line.strip()}", file=sys.stderr, flush=True)

            # Re-apply the fragment and retry olddefconfig once.
            # This can help if the issue is a circular dependency that
            # resolves differently on the second pass.
            print(f"    Re-applying fragment and retrying olddefconfig...",
                  file=sys.stderr, flush=True)

            err = _apply_config_fragment(build_config)
            if err:
                return err

            cmd = _make_cmd("olddefconfig", arch=arch, source_dir=source_dir)
            cmd.append(f"O={build_dir}")
            r = run_cmd(cmd, timeout=60)
            if not r["ok"]:
                return {"ok": False, "error": f"olddefconfig (retry) failed: {r.get('stderr', '')[:300]}"}

            # Check again
            final_config = build_config.read_text()
            still_missing = [c for c in missing if f"{c}=y" not in final_config]
            if still_missing:
                print(f"    ⚠  Still missing after retry: {', '.join(still_missing)}",
                      file=sys.stderr, flush=True)
                for cfg in still_missing:
                    for line in final_config.splitlines():
                        if cfg in line:
                            print(f"    ⚠  {line.strip()}", file=sys.stderr, flush=True)
                print(f"    These configs have unmet dependencies or conflicts.",
                      file=sys.stderr, flush=True)
                # Don't fail — the build can proceed without KUnit.
                # The tester will report it as a skip.

        # Build the kernel Image (out-of-tree).
        # Use -C <source> so host tools are built in O= (tmpfs), not
        # the source tree (bind mount).
        cmd = _make_cmd("Image", arch=arch, source_dir=source_dir)
        cmd.append(f"O={build_dir}")

        run_id = str(uuid.uuid4())[:8]
        # Artifacts dir: build/ for normal, build-kunit/ for kunit builds
        build_subdir = "build-kunit" if kunit else "build"
        artifacts_build_dir = WORKSPACE / "artifacts" / task_id / role / build_subdir
        artifacts_build_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(artifacts_build_dir / "build.log")

        result = run_cmd_streaming(
            cmd, timeout=1800, prefix="    ",
            log_file=log_path,
        )
        result["run_id"] = run_id

        # ccache stats after build
        ccache_after = _ccache_stats()
        if ccache_after.get("available"):
            print(f"    ℹ  ccache after:  {ccache_after.get('hits', '?')} | "
                  f"{ccache_after.get('misses', '?')} | "
                  f"{ccache_after.get('cache_size', '?')}",
                  file=sys.stderr, flush=True)

        # Log build result and build dir contents for diagnostics
        print(f"    ℹ  Build result: ok={result.get('ok')}, code={result.get('code')}",
              file=sys.stderr, flush=True)
        print(f"    ℹ  Build dir: {build_dir}", file=sys.stderr, flush=True)
        if Path(build_dir).exists():
            # List key files that should exist after a successful build
            key_files = [
                "arch/arm64/boot/Image",
                ".config",
                "System.map",
                "vmlinux",
            ]
            for kf in key_files:
                p = Path(build_dir) / kf
                exists = p.exists()
                size = p.stat().st_size if exists else 0
                print(f"    ℹ  {kf}: exists={exists}, size={size}",
                      file=sys.stderr, flush=True)
        else:
            print(f"    ⚠  Build dir {build_dir} does not exist!",
                  file=sys.stderr, flush=True)

        # ── Copy artifacts to host-visible directory ──────────────
        # Copy whatever exists, even if the build failed — partial
        # artifacts are useful for debugging.
        # New layout: artifacts/<task-id>/build/
        artifacts_dir = WORKSPACE / "artifacts" / task_id / role / build_subdir
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Files needed for QEMU boot and debugging
        # Always include vmlinux — needed for debugging and the build
        # tree tarball is preserved separately.
        files_to_copy = [
            ("arch/arm64/boot/Image", "Image"),
            (".config", ".config"),
            ("System.map", "System.map"),
            ("vmlinux", "vmlinux"),
            ("modules.builtin", "modules.builtin"),
            ("Module.symvers", "Module.symvers"),
        ]

        copied = []
        for src_rel, dst_name in files_to_copy:
            src = Path(build_dir) / src_rel
            dst = artifacts_dir / dst_name
            if src.exists():
                shutil.copy2(src, dst)
                copied.append(dst_name)
            else:
                print(f"    ⚠  Not copying {dst_name}: {src} does not exist",
                      file=sys.stderr, flush=True)

        # ── Preserve build tree metadata (not .o files) ──────────
        # Save a SMALL tarball with just the generated headers, .cmd
        # files, and config — NOT the 3GB of .o files (they're in
        # ccache and can be regenerated). This keeps the tarball small
        # (~50MB) and fast to write through VirtioFS.
        # Set include_vmlinux=True for the full vmlinux (already copied above).
        print(f"    ℹ  Preserving build metadata...",
              file=sys.stderr, flush=True)
        tarball_path = artifacts_dir / "build-metadata.tar.gz"
        # Exclude .o files (90% of build tree size, regenerable via ccache)
        tar_cmd = ["tar", "czf", str(tarball_path),
                   "-C", build_dir,
                   "--exclude=*.o",
                   "--exclude=*.o.cmd",
                   "--exclude=*.a",
                   "--exclude=vmlinux",
                   "--exclude=modules.builtin.modinfo",
                   "."]
        tar_r = run_cmd(tar_cmd, timeout=120)
        if tar_r["ok"]:
            tar_size = tarball_path.stat().st_size
            print(f"    ℹ  Build metadata: {tarball_path} "
                  f"({tar_size // (1024*1024)}MB)",
                  file=sys.stderr, flush=True)
            copied.append("build-metadata.tar.gz")
        else:
            print(f"    ⚠  Failed to create build metadata tarball: "
                  f"{tar_r.get('stderr', '')[:200]}",
                  file=sys.stderr, flush=True)

        # Write build info (always, even on failure)
        git_rev = run_cmd(["git", "-C", cwd, "rev-parse", "HEAD"], timeout=5)
        git_rev_str = git_rev.get("stdout", "unknown").strip()[:12]
        build_info = (
            f"Kernel Build Info\n"
            f"==================\n"
            f"Date: {datetime.datetime.now().isoformat()}\n"
            f"Task ID: {task_id}\n"
            f"Worktree: {cwd}\n"
            f"Build dir: {build_dir} (tmpfs, out-of-tree)\n"
            f"Git revision: {git_rev_str}\n"
            f"Architecture: {arch or 'arm64'}\n"
            f"Compiler: {os.environ.get('CC', 'ccache clang')}\n"
            f"LLVM: {os.environ.get('KAG_LLVM', '1')}\n"
            f"Build succeeded: {result.get('ok', False)}\n"
            f"Files: {', '.join(copied) if copied else '(none)'}\n"
        )
        (artifacts_dir / "build-info.txt").write_text(build_info)

        result["artifacts_dir"] = str(artifacts_dir)
        result["kernel_path"] = str(artifacts_dir / "Image") if "Image" in copied else None
        result["build_dir"] = build_dir
        result["copied_files"] = copied
        result["fast"] = fast
        result["ccache_before"] = ccache_before
        result["ccache_after"] = ccache_after

        if not result.get("ok"):
            # Include the last part of the build log in the error
            log_tail = ""
            try:
                log_tail = Path(log_path).read_text()[-1500:]
            except Exception:
                pass
            result["error"] = (
                f"Build failed (code={result.get('code')}). "
                f"Build log: {log_path}\n"
                f"Log tail:\n{log_tail}"
            )
            return result

        return result

    def make_modules(worktree: str = None, arch: str = None) -> dict:
        """Build kernel modules (out-of-tree)."""
        cwd = _resolve_worktree(worktree)
        source_dir = _get_source_dir(worktree)
        build_dir = _get_build_dir(worktree, kunit=False)
        cmd = _make_cmd("modules", arch=arch, source_dir=source_dir)
        cmd.append(f"O={build_dir}")
        return run_cmd(cmd, timeout=900)

    def _ensure_build_dir_ready(worktree: str, arch: str = None) -> dict | None:
        """Ensure the out-of-tree build dir is set up for compilation.

        Runs defconfig + config fragment + olddefconfig + prepare if
        the build dir isn't ready yet. This is called by make_file
        so the implementer can do quick compile checks without
        running a full make_image first.

        Returns None if ready (or successfully set up), or an error
        dict on failure.
        """
        cwd = _resolve_worktree(worktree)
        source_dir = _get_source_dir(worktree)
        build_dir = _get_build_dir(worktree, kunit=False)

        # Check if build dir is already prepared (has generated headers)
        prepared_marker = Path(build_dir) / "include" / "generated" / "autoconf.h"
        if prepared_marker.exists():
            return None  # Already ready

        # Need to set up the build dir. Run defconfig first.
        # Use -C <source> so host tools go to O= (tmpfs), not source tree.
        build_config = Path(build_dir) / ".config"
        if not build_config.exists():
            wt_config = Path(cwd) / ".config"
            if wt_config.exists():
                shutil.copy(wt_config, build_config)
            else:
                cmd = _make_cmd("defconfig", arch=arch, source_dir=source_dir)
                cmd.append(f"O={build_dir}")
                r = run_cmd(cmd, timeout=60)
                if not r["ok"]:
                    return {"ok": False, "error": f"defconfig failed: {r.get('stderr', '')[:300]}"}

        # Apply config fragment
        err = _apply_config_fragment(build_config)
        if err:
            return err

        # Run olddefconfig
        cmd = _make_cmd("olddefconfig", arch=arch, source_dir=source_dir)
        cmd.append(f"O={build_dir}")
        r = run_cmd(cmd, timeout=60)
        if not r["ok"]:
            return {"ok": False, "error": f"olddefconfig failed: {r.get('stderr', '')[:300]}"}

        # Run prepare + modules_prepare to generate headers
        cmd = _make_cmd("prepare", arch=arch, source_dir=source_dir)
        cmd.append(f"O={build_dir}")
        r = run_cmd(cmd, timeout=120)
        if not r["ok"]:
            return {"ok": False, "error": f"make prepare failed: {r.get('stderr', '')[:300]}"}

        return None

    def make_file(filepath: str, worktree: str = None,
                  arch: str = None) -> dict:
        """Build a single object file (fast inner loop).

        Uses the same out-of-tree build dir as make_image. If the build
        dir isn't set up yet (first call in this container session),
        automatically runs defconfig + prepare to set it up (~30-60s
        one-time cost). Subsequent calls are <1s with warm ccache.
        """
        cwd = _resolve_worktree(worktree)
        source_dir = _get_source_dir(worktree)

        # Auto-setup build dir if needed
        setup_err = _ensure_build_dir_ready(worktree, arch=arch)
        if setup_err:
            return setup_err

        build_dir = _get_build_dir(worktree, kunit=False)

        if filepath.endswith(".c"):
            obj = filepath[:-2] + ".o"
        else:
            obj = filepath
        cmd = _make_cmd(obj, arch=arch, source_dir=source_dir)
        cmd.append(f"O={build_dir}")
        return run_cmd(cmd, timeout=120)

    def make_defconfig(worktree: str = None, arch: str = None) -> dict:
        """Run make defconfig (out-of-tree in build dir)."""
        cwd = _resolve_worktree(worktree)
        source_dir = _get_source_dir(worktree)
        build_dir = _get_build_dir(worktree, kunit=False)
        cmd = _make_cmd("defconfig", arch=arch, source_dir=source_dir)
        cmd.append(f"O={build_dir}")
        return run_cmd(cmd, timeout=60)

    def make_olddefconfig(worktree: str = None, arch: str = None) -> dict:
        """Run make olddefconfig (out-of-tree in build dir).

        Re-applies the subsystem config fragment first, then runs
        olddefconfig to resolve dependencies. This ensures subsystem
        configs (like CONFIG_KUNIT) are present even if a prior
        olddefconfig turned them off.
        """
        cwd = _resolve_worktree(worktree)
        source_dir = _get_source_dir(worktree)
        build_dir = _get_build_dir(worktree, kunit=False)
        build_config = Path(build_dir) / ".config"

        # Re-apply the config fragment before olddefconfig
        if build_config.exists():
            _apply_config_fragment(build_config)

        cmd = _make_cmd("olddefconfig", arch=arch, source_dir=source_dir)
        cmd.append(f"O={build_dir}")
        return run_cmd(cmd, timeout=60)

    def clean_source_tree(worktree: str = None, arch: str = None) -> dict:
        """Remove stale in-tree build artifacts from the source tree.

        Runs 'make mrproper' in the worktree to remove all generated
        files (.o, .cmd, vmlinux, fixdep, .config, etc.). This is
        needed if the source tree was polluted by v0.1.x in-tree builds.

        WARNING: This removes .config too. Run make_image afterwards
        to regenerate it.
        """
        cwd = _resolve_worktree(worktree)
        source_dir = _get_source_dir(worktree)
        cmd = _make_cmd("mrproper", arch=arch, source_dir=source_dir)
        return run_cmd(cmd, timeout=120)

    return [
        Tool(
            name="make_image",
            description=(
                "Build the kernel Image using out-of-tree build on tmpfs. "
                "Copies Image, .config, System.map to "
                "/workspace/artifacts/<task-id>/<role>/build/. "
                "Pass worktree=<worktree_path from ticket>. "
                "Set kunit=true to also enable CONFIG_KUNIT for KUnit tests."
            ),
            fn=make_image,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string",
                                 "description": "Full path to worktree (from ticket)"},
                    "arch": {"type": "string", "default": "arm64"},
                    "include_vmlinux": {"type": "boolean", "default": False},
                    "fast": {"type": "boolean", "default": False,
                             "description": "Ignored (backward compat)."},
                    "kunit": {"type": "boolean", "default": False,
                             "description": "Enable CONFIG_KUNIT for KUnit tests at boot."},
                },
            },
        ),
        Tool(
            name="make_modules",
            description="Build kernel modules (out-of-tree).",
            fn=make_modules,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string"},
                    "arch": {"type": "string"},
                },
            },
        ),
        Tool(
            name="make_file",
            description=(
                "Build a single .o file (fast inner loop, <1s with warm "
                "ccache). Uses the out-of-tree build dir from make_image. "
                "Run make_image first in the same session."
            ),
            fn=make_file,
            schema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string",
                                 "description": "e.g. 'mm/page_alloc.c'"},
                    "worktree": {"type": "string"},
                    "arch": {"type": "string"},
                },
                "required": ["filepath"],
            },
        ),
        Tool(
            name="make_defconfig",
            description="Run make defconfig (out-of-tree in build dir).",
            fn=make_defconfig,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string"},
                    "arch": {"type": "string"},
                },
            },
        ),
        Tool(
            name="make_olddefconfig",
            description=(
                "Run make olddefconfig (out-of-tree in build dir). "
                "Re-applies the subsystem config fragment first, then "
                "resolves dependencies. Use this if CONFIG_KUNIT or "
                "other subsystem configs are missing from the .config."
            ),
            fn=make_olddefconfig,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string"},
                    "arch": {"type": "string"},
                },
            },
        ),
        Tool(
            name="clean_source_tree",
            description=(
                "Remove stale in-tree build artifacts from the source tree "
                "(runs make mrproper). Use this if the source tree has "
                ".o, vmlinux, fixdep, or other build artifacts from "
                "previous in-tree builds. WARNING: removes .config too."
            ),
            fn=clean_source_tree,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string"},
                    "arch": {"type": "string"},
                },
            },
        ),
    ]
