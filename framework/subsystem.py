"""Subsystem profile protocol and loader.

A subsystem profile defines everything the framework needs to know about
a kernel subsystem (mm, sched, block, fs, ...) without the framework
hardcoding any of it. The framework consumes the protocol; subsystems
implement it.
"""
from __future__ import annotations
from pathlib import Path
from typing import Protocol, runtime_checkable, Any
import importlib.util
import sys


def _get_subsystems_dir() -> Path:
    """Get the subsystems directory, checking common locations."""
    # Check env var first
    env_dir = Path("/opt/subsystems")
    # Also check relative to this file for development
    dev_dir = Path(__file__).parent.parent / "subsystems"
    if dev_dir.exists():
        return dev_dir
    return env_dir


@runtime_checkable
class SubsystemProfile(Protocol):
    """Interface every subsystem must implement."""

    # ─── Identity ───────────────────────────────────────────────────
    name: str
    display_name: str
    commit_prefix: str

    # ─── Source scope ───────────────────────────────────────────────
    source_paths: list[str]

    # ─── Test configuration ────────────────────────────────────────
    kunit_filter: str
    ltp_categories: list[str]

    # ─── Build configuration ───────────────────────────────────────
    kconfig_overrides: dict[str, bool]

    # ─── QEMU ───────────────────────────────────────────────────────
    qemu_extra_args: list[str]

    # ─── Prompts ────────────────────────────────────────────────────
    coding_conventions: str
    reviewer_extra_checks: list[str]
    test_exit_criteria: list[str]

    # ─── Validation ─────────────────────────────────────────────────
    def validate_patch_scope(self, changed_files: list[str]) -> list[str]: ...


def load_subsystem(name: str) -> SubsystemProfile:
    """Dynamically load a subsystem profile by name."""
    subsystems_dir = _get_subsystems_dir()
    profile_path = subsystems_dir / name / "profile.py"

    if not profile_path.exists():
        available = list_subsystems()
        raise ValueError(
            f"Unknown subsystem: {name!r}. Available: {available}"
        )

    # Use a unique module name to avoid conflicts
    mod_name = f"subsystems.{name}.profile"
    if mod_name in sys.modules:
        module = sys.modules[mod_name]
    else:
        spec = importlib.util.spec_from_file_location(mod_name, profile_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load spec for {profile_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

    if not hasattr(module, "profile"):
        raise AttributeError(
            f"subsystems/{name}/profile.py must define `profile`"
        )

    return module.profile


def list_subsystems() -> list[str]:
    """List all available subsystem names."""
    subsystems_dir = _get_subsystems_dir()
    if not subsystems_dir.exists():
        return []
    return sorted(
        p.name
        for p in subsystems_dir.iterdir()
        if p.is_dir() and (p / "profile.py").exists()
    )
