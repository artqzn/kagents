"""Filesystem subsystem profile (stub — extend before use)."""
from __future__ import annotations
from pathlib import Path

SUBSYSTEM_DIR = Path(__file__).parent


class FsProfile:
    """Filesystem (fs) subsystem profile."""

    name = "fs"
    display_name = "Filesystem"
    commit_prefix = "fs"

    source_paths = [
        "fs/",
        "include/linux/fs.h",
        "include/linux/fs_types.h",
        "include/linux/dcache.h",
        "include/linux/inode.h",
        "include/linux/file.h",
        "include/linux/folio.h",  # used by page cache
    ]

    kunit_filter = "ext4"  # KUnit suites exist for some filesystems
    ltp_categories = ["fs"]

    kconfig_overrides = {
        "CONFIG_EXT4_FS": True,
        "CONFIG_EXT4_FS_POSIX_ACL": True,
        "CONFIG_BTRFS_FS": True,
        "CONFIG_XFS_FS": True,
        "CONFIG_F2FS_FS": True,
    }

    qemu_extra_args = []

    @property
    def coding_conventions(self) -> str:
        path = SUBSYSTEM_DIR / "conventions.md"
        return path.read_text() if path.exists() else ""

    @property
    def reviewer_extra_checks(self) -> list[str]:
        return []

    test_exit_criteria = [
        "Kernel boots and mounts ext4 rootfs",
        "LTP fs category passes on selected subset",
        "No new sparse warnings",
        "No checkpatch errors",
    ]

    def validate_patch_scope(self, changed_files: list[str]) -> list[str]:
        out = []
        for f in changed_files:
            if not any(
                f.startswith(p.rstrip("/")) for p in self.source_paths
            ):
                out.append(f)
        return out


profile = FsProfile()
