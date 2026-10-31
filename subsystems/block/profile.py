"""Block I/O subsystem profile (stub — extend before use)."""
from __future__ import annotations
from pathlib import Path

SUBSYSTEM_DIR = Path(__file__).parent


class BlockProfile:
    """Block I/O (block) subsystem profile."""

    name = "block"
    display_name = "Block I/O"
    commit_prefix = "block"

    source_paths = [
        "block/",
        "include/linux/blkdev.h",
        "include/linux/blk-mq.h",
        "include/linux/bio.h",
        "include/linux/part_stat.h",
    ]

    kunit_filter = ""  # Limited KUnit coverage for block
    ltp_categories = ["block"]

    kconfig_overrides = {
        "CONFIG_BLOCK": True,
        "CONFIG_BLK_DEV_THROTTLING": True,
        "CONFIG_BLK_WBT": True,
        "CONFIG_BLK_CGROUP": True,
        "CONFIG_MQ_IOSCHED_DEADLINE": True,
        "CONFIG_MQ_IOSCHED_KYBER": True,
        "CONFIG_MQ_IOSCHED_BFQ": True,
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
        "Kernel boots and detects virtio-blk",
        "LTP block category passes on selected subset",
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


profile = BlockProfile()
