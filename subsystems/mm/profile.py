"""Memory Management subsystem profile.

This is the reference implementation and default subsystem. Teams adding
new subsystems should use this as a template.
"""
from __future__ import annotations
from pathlib import Path

SUBSYSTEM_DIR = Path(__file__).parent


class MMProfile:
    """Memory Management (mm) subsystem profile."""

    # ─── Identity ───────────────────────────────────────────────────
    name = "mm"
    display_name = "Memory Management"
    commit_prefix = "mm"

    # ─── Source scope ───────────────────────────────────────────────
    source_paths = [
        "mm/",
        "include/linux/mm.h",
        "include/linux/mmzone.h",
        "include/linux/page-flags.h",
        "include/linux/slab.h",
        "include/linux/gfp.h",
        "include/linux/migrate.h",
        "include/linux/huge_mm.h",
        "include/linux/memcontrol.h",
        "include/linux/compaction.h",
        "include/linux/vmstat.h",
        "include/linux/page_ref.h",
        "include/linux/folio.h",
    ]

    # ─── Test configuration ────────────────────────────────────────
    kunit_filter = "mm"  # matches mm/slub, mm/page_alloc, mm/memblock, etc.
    ltp_categories = ["mm", "hugetlb"]

    # ─── Build configuration ───────────────────────────────────────
    kconfig_overrides = {
        "CONFIG_TRANSPARENT_HUGEPAGE": True,
        "CONFIG_HUGETLBFS": True,
        "CONFIG_MEMCG": True,
        "CONFIG_MEMCG_SWAP": True,
        "CONFIG_ZSWAP": True,
        "CONFIG_ZRAM": True,
        "CONFIG_KUNIT": True,
        "CONFIG_KUNIT_ALL_TESTS": True,
        "CONFIG_DEBUG_VM": True,
        "CONFIG_DEBUG_PAGEALLOC": True,
        "CONFIG_KASAN": True,
        "CONFIG_KASAN_GENERIC": True,
        "CONFIG_UBSAN": True,
        "CONFIG_SLUB_DEBUG": True,
        # MTE deliberately off — TCG does not emulate
        "CONFIG_ARM64_MTE": False,
    }

    # ─── QEMU ───────────────────────────────────────────────────────
    qemu_extra_args = ["kunit.enable=1"]

    # ─── Prompts ────────────────────────────────────────────────────
    @property
    def coding_conventions(self) -> str:
        path = SUBSYSTEM_DIR / "conventions.md"
        return path.read_text() if path.exists() else ""

    @property
    def reviewer_extra_checks(self) -> list[str]:
        path = SUBSYSTEM_DIR / "agents" / "reviewer" / "checks.md"
        if path.exists():
            return [
                line
                for line in path.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            ]
        return []

    # ─── Test exit criteria ─────────────────────────────────────────
    test_exit_criteria = [
        "All KUnit suites matching 'mm' pass",
        "LTP categories mm + hugetlb pass on selected subset",
        "Kernel boots to initramfs shell within 30s under TCG",
        "No new sparse warnings on touched files",
        "No checkpatch --strict errors",
    ]

    # ─── Validation ─────────────────────────────────────────────────
    def validate_patch_scope(self, changed_files: list[str]) -> list[str]:
        """Return list of files that are outside source_paths."""
        out_of_scope = []
        for f in changed_files:
            in_scope = any(
                f.startswith(p.rstrip("/")) or f == p
                for p in self.source_paths
            )
            if not in_scope:
                out_of_scope.append(f)
        return out_of_scope


profile = MMProfile()
