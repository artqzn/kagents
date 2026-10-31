"""Scheduler subsystem profile (stub — extend before use)."""
from __future__ import annotations
from pathlib import Path

SUBSYSTEM_DIR = Path(__file__).parent


class SchedProfile:
    """Scheduler (sched) subsystem profile."""

    name = "sched"
    display_name = "Scheduler"
    commit_prefix = "sched"

    source_paths = [
        "kernel/sched/",
        "include/linux/sched.h",
        "include/linux/sched/",
        "include/uapi/linux/sched.h",
    ]

    kunit_filter = ""  # No KUnit suites for sched yet
    ltp_categories = ["sched"]

    kconfig_overrides = {
        "CONFIG_SCHED_DEBUG": True,
        "CONFIG_SCHEDSTATS": True,
        "CONFIG_SCHED_TRACER": True,
        "CONFIG_CGROUP_SCHED": True,
        "CONFIG_FAIR_GROUP_SCHED": True,
        "CONFIG_RT_GROUP_SCHED": True,
    }

    qemu_extra_args = ["sched_debug"]

    @property
    def coding_conventions(self) -> str:
        path = SUBSYSTEM_DIR / "conventions.md"
        return path.read_text() if path.exists() else ""

    @property
    def reviewer_extra_checks(self) -> list[str]:
        path = SUBSYSTEM_DIR / "agents" / "reviewer" / "checks.md"
        if path.exists():
            return [
                line for line in path.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            ]
        return []

    test_exit_criteria = [
        "Kernel boots and runs initramfs shell within 30s",
        "LTP sched category passes on selected subset",
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


profile = SchedProfile()
