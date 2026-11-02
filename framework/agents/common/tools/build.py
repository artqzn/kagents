"""Build tools — out-of-tree kernel compilation on tmpfs.

Architecture:
    Source tree (worktree, bind-mounted from host)
        │
        │  make O=/tmp/kbuild-<hash> Image
        ▼
    Build dir (/tmp/kbuild-<hash>, tmpfs = RAM-backed)
        │
        │  Copy Image, .config, System.map, vmlinux, etc.
        ▼
    Artifacts dir (/workspace/artifacts/builds/<task-id>/, host-visible)

Benefits:
    - Build artifacts written to RAM (tmpfs), not through VirtioFS → 3-5× faster
    - Source tree stays clean → no kunit.py "source tree not clean" errors
    - Artifacts on host → all containers share the same build output
    - ccache persists across containers (named volume)
"""
from __future__ import annotations
import os
import platform
import hashlib
import shutil
import datetime
from pathlib import Path
from framework.agents.common.tools.base import Tool, run_cmd, run_cmd_streaming, WORKSPACE


def tools(subsystem) -> list[Tool]:
    """Create build tools."""

    def _resolve_worktree(worktree: str = None) -> str:
        """Resolve worktree path to an absolute path."""
        if not worktree or worktree == "None":
            return str(WORKSPACE / "src")
        p = Path(worktree)
        if not p.is_absolute():
            p = WORKSPACE / p
        resolved = str(p)
        if not Path(resolved).exists():
            src = str(WORKSPACE / "src")
            if Path(src).exists():
                return src
        return resolved

    def _get_build_dir(worktree: str) -> str:
        """Get the out-of-tree build directory for a worktree.

        Uses /tmp (tmpfs, RAM-backed) with a hash of the worktree path
        so different tasks don't collide. The directory is created if
        it doesn't exist.
        """
        cwd = _resolve_worktree(worktree)
        wt_hash = hashlib.md5(cwd.encode()).hexdigest()[:8]
        build_dir = f"/tmp/kbuild-{wt_hash}"
        Path(build_dir).mkdir(parents=True, exist_ok=True)
        return build_dir

    def _make_cmd(target: str, arch: str = None, jobs: int = None,
                  cross_compile: bool = None,
                  source_dir: str = None) -> list[str]:
        """Build a make command with ccache, proper arch and cross-compile.

        Uses -C <source_dir> so that make reads Makefiles from the
        source tree but writes all output (including host tools like
        fixdep) to the O= build directory. This is critical for
        out-of-tree builds on bind mounts — without -C, kbuild
        builds fixdep in the source tree, which can fail with
        "Permission denied" on bind mounts (VirtioFS, overlayfs).

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
            cmd.append('CC=ccache clang')

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
        config_fragment = None
        sub_dir = getattr(subsystem, "SUBSYSTEM_DIR", None) if subsystem else None
        if sub_dir:
            cf = Path(str(sub_dir)) / "config-fragment.config"
            if cf.exists():
                config_fragment = cf
        if not config_fragment:
            return None

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

    def make_image(worktree: str = None, arch: str = None,
                   include_vmlinux: bool = False) -> dict:
        """Build the kernel Image using out-of-tree build on tmpfs.

        Build artifacts are written to /tmp/kbuild-<hash> (tmpfs,
        RAM-backed) for maximum I/O speed. After the build, the kernel
        Image and debug files are copied to
        /workspace/artifacts/builds/<task-id>/ on the host filesystem,
        where they can be shared with all other containers.

        The source tree remains clean (no in-tree build artifacts).

        Args:
            worktree: Path to the git worktree (from ticket).
            arch: Target architecture (default: arm64).
            include_vmlinux: If True, copy vmlinux to artifacts for
                             gdb debugging. vmlinux is large
                             (~500MB-1GB with debug info), so it's
                             off by default.

        Returns dict with:
            - ok: True/False
            - artifacts_dir: path to the artifacts directory on host
            - kernel_path: path to the kernel Image in artifacts
            - build_dir: path to the tmpfs build dir (ephemeral)
            - copied_files: list of files copied to artifacts
        """
        import uuid
        # Normalize boolean — LLMs sometimes pass "true"/"false" strings
        if isinstance(include_vmlinux, str):
            include_vmlinux = include_vmlinux.lower() in ("true", "1", "yes")
        cwd = _resolve_worktree(worktree)
        task_id = Path(cwd).name

        # Out-of-tree build directory on tmpfs (RAM-backed)
        build_dir = _get_build_dir(worktree)

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
                cmd = _make_cmd("defconfig", arch=arch, source_dir=cwd)
                cmd.append(f"O={build_dir}")
                r = run_cmd(cmd, timeout=60)  # cwd doesn't matter with -C
                if not r["ok"]:
                    return r

        # Apply subsystem config fragment to the build dir's .config
        err = _apply_config_fragment(build_config)
        if err:
            return err

        # Run olddefconfig to resolve all dependencies
        cmd = _make_cmd("olddefconfig", arch=arch, source_dir=cwd)
        cmd.append(f"O={build_dir}")
        r = run_cmd(cmd, timeout=60)
        if not r["ok"]:
            return {"ok": False, "error": f"olddefconfig failed: {r.get('stderr', '')[:300]}"}

        # ── Verify critical config options survived olddefconfig ──
        # olddefconfig can turn off config options that have unmet
        # dependencies. If the subsystem fragment set CONFIG_KUNIT=y
        # but olddefconfig turned it off, print diagnostics so we can
        # understand why.
        final_config = build_config.read_text()
        critical_configs = []
        sub_dir = getattr(subsystem, "SUBSYSTEM_DIR", None) if subsystem else None
        if sub_dir:
            cf = Path(str(sub_dir)) / "config-fragment.config"
            if cf.exists():
                for line in cf.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("CONFIG_") and "=y" in line:
                        critical_configs.append(line.split("=")[0].strip())

        missing = []
        for cfg in critical_configs:
            if f"{cfg}=y" not in final_config:
                missing.append(cfg)

        if missing:
            import sys as _sys
            print(f"    ⚠  Config options turned off by olddefconfig: "
                  f"{', '.join(missing)}", file=_sys.stderr, flush=True)

            # Print diagnostic: show what olddefconfig did to each missing config
            for cfg in missing:
                for line in final_config.splitlines():
                    if cfg in line:
                        print(f"    ⚠  {line.strip()}", file=_sys.stderr, flush=True)

            # Re-apply the fragment and retry olddefconfig once.
            # This can help if the issue is a circular dependency that
            # resolves differently on the second pass.
            print(f"    Re-applying fragment and retrying olddefconfig...",
                  file=_sys.stderr, flush=True)

            err = _apply_config_fragment(build_config)
            if err:
                return err

            cmd = _make_cmd("olddefconfig", arch=arch, source_dir=cwd)
            cmd.append(f"O={build_dir}")
            r = run_cmd(cmd, timeout=60)
            if not r["ok"]:
                return {"ok": False, "error": f"olddefconfig (retry) failed: {r.get('stderr', '')[:300]}"}

            # Check again
            final_config = build_config.read_text()
            still_missing = [c for c in missing if f"{c}=y" not in final_config]
            if still_missing:
                print(f"    ⚠  Still missing after retry: {', '.join(still_missing)}",
                      file=_sys.stderr, flush=True)
                for cfg in still_missing:
                    for line in final_config.splitlines():
                        if cfg in line:
                            print(f"    ⚠  {line.strip()}", file=_sys.stderr, flush=True)
                print(f"    These configs have unmet dependencies or conflicts.",
                      file=_sys.stderr, flush=True)
                # Don't fail — the build can proceed without KUnit.
                # The tester will report it as a skip.

        # Build the kernel Image (out-of-tree).
        # Use -C <source> so host tools are built in O= (tmpfs), not
        # the source tree (bind mount).
        cmd = _make_cmd("Image", arch=arch, source_dir=cwd)
        cmd.append(f"O={build_dir}")

        run_id = str(uuid.uuid4())[:8]
        log_path = str(WORKSPACE / "artifacts" / "test-results" / run_id / "build.log")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)

        result = run_cmd_streaming(
            cmd, timeout=1800, prefix="    ",
            log_file=log_path,
        )
        result["run_id"] = run_id

        if not result.get("ok"):
            return result

        # ── Copy artifacts to host-visible directory ──────────────
        artifacts_dir = WORKSPACE / "artifacts" / "builds" / task_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Files needed for QEMU boot and debugging
        files_to_copy = [
            ("arch/arm64/boot/Image", "Image"),
            (".config", ".config"),
            ("System.map", "System.map"),
            ("modules.builtin", "modules.builtin"),
            ("Module.symvers", "Module.symvers"),
        ]
        if include_vmlinux:
            files_to_copy.append(("vmlinux", "vmlinux"))

        copied = []
        for src_rel, dst_name in files_to_copy:
            src = Path(build_dir) / src_rel
            dst = artifacts_dir / dst_name
            if src.exists():
                shutil.copy2(src, dst)
                copied.append(dst_name)

        # Write build info
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
            f"Files: {', '.join(copied)}\n"
        )
        (artifacts_dir / "build-info.txt").write_text(build_info)

        result["artifacts_dir"] = str(artifacts_dir)
        result["kernel_path"] = str(artifacts_dir / "Image")
        result["build_dir"] = build_dir
        result["copied_files"] = copied

        return result

    def make_modules(worktree: str = None, arch: str = None) -> dict:
        """Build kernel modules (out-of-tree)."""
        cwd = _resolve_worktree(worktree)
        build_dir = _get_build_dir(worktree)
        cmd = _make_cmd("modules", arch=arch, source_dir=cwd)
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
        build_dir = _get_build_dir(worktree)

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
                cmd = _make_cmd("defconfig", arch=arch, source_dir=cwd)
                cmd.append(f"O={build_dir}")
                r = run_cmd(cmd, timeout=60)
                if not r["ok"]:
                    return {"ok": False, "error": f"defconfig failed: {r.get('stderr', '')[:300]}"}

        # Apply config fragment
        err = _apply_config_fragment(build_config)
        if err:
            return err

        # Run olddefconfig
        cmd = _make_cmd("olddefconfig", arch=arch, source_dir=cwd)
        cmd.append(f"O={build_dir}")
        r = run_cmd(cmd, timeout=60)
        if not r["ok"]:
            return {"ok": False, "error": f"olddefconfig failed: {r.get('stderr', '')[:300]}"}

        # Run prepare + modules_prepare to generate headers
        cmd = _make_cmd("prepare", arch=arch, source_dir=cwd)
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

        # Auto-setup build dir if needed
        setup_err = _ensure_build_dir_ready(worktree, arch=arch)
        if setup_err:
            return setup_err

        build_dir = _get_build_dir(worktree)

        if filepath.endswith(".c"):
            obj = filepath[:-2] + ".o"
        else:
            obj = filepath
        cmd = _make_cmd(obj, arch=arch, source_dir=cwd)
        cmd.append(f"O={build_dir}")
        return run_cmd(cmd, timeout=120)

    def make_defconfig(worktree: str = None, arch: str = None) -> dict:
        """Run make defconfig (out-of-tree in build dir)."""
        cwd = _resolve_worktree(worktree)
        build_dir = _get_build_dir(worktree)
        cmd = _make_cmd("defconfig", arch=arch, source_dir=cwd)
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
        build_dir = _get_build_dir(worktree)
        build_config = Path(build_dir) / ".config"

        # Re-apply the config fragment before olddefconfig
        if build_config.exists():
            _apply_config_fragment(build_config)

        cmd = _make_cmd("olddefconfig", arch=arch, source_dir=cwd)
        cmd.append(f"O={build_dir}")
        return run_cmd(cmd, timeout=60)

    return [
        Tool(
            name="make_image",
            description=(
                "Build the kernel Image using out-of-tree build on tmpfs "
                "(RAM-backed, 3-5× faster than in-tree). After building, "
                "copies Image, .config, System.map to "
                "/workspace/artifacts/builds/<task-id>/ for sharing with "
                "other containers. The source tree stays clean. "
                "Pass worktree=<worktree_path from ticket>. "
                "Set include_vmlinux=true if you need gdb debugging "
                "(vmlinux is large, ~500MB-1GB)."
            ),
            fn=make_image,
            schema={
                "type": "object",
                "properties": {
                    "worktree": {"type": "string",
                                 "description": "Full path to worktree (from ticket)"},
                    "arch": {"type": "string", "default": "arm64"},
                    "include_vmlinux": {"type": "boolean", "default": False},
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
    ]
