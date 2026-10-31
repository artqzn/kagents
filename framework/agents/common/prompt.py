"""Layered prompt resolution.

For any (subsystem, role) combination, resolves the system prompt via
three-level lookup:

1. Full override: subsystems/<name>/agents/<role>/prompt.md
2. Layered: prefix + framework/agents/<role>/prompt.md + suffix
3. Default: framework/agents/<role>/prompt.md
"""
from __future__ import annotations
from pathlib import Path
import importlib.util
import sys


def _get_framework_dir() -> Path:
    """Get the framework directory."""
    # Check development path first
    dev_path = Path(__file__).parent.parent.parent.parent
    if (dev_path / "framework" / "agents").exists():
        return dev_path / "framework"
    return Path("/opt/framework")


def _get_subsystems_dir() -> Path:
    """Get the subsystems directory."""
    dev_path = Path(__file__).parent.parent.parent.parent
    if (dev_path / "subsystems").exists():
        return dev_path / "subsystems"
    return Path("/opt/subsystems")


FRAMEWORK = _get_framework_dir()
SUBSYSTEMS = _get_subsystems_dir()


def resolve_prompt(role: str, subsystem_name: str) -> tuple[str, str]:
    """Resolve system prompt for (subsystem, role).

    Returns (prompt_text, source_description).
    """
    sub_dir = SUBSYSTEMS / subsystem_name / "agents" / role
    fw_prompt = FRAMEWORK / "agents" / role / "prompt.md"

    # Level 1: full override
    full_override = sub_dir / "prompt.md"
    if full_override.exists():
        return full_override.read_text(), f"override:{full_override}"

    # Level 2: layered
    if not fw_prompt.exists():
        raise FileNotFoundError(
            f"No prompt for role '{role}'. "
            f"Looked in: {full_override}, {fw_prompt}"
        )

    parts = []

    # Optional prefix
    prefix = sub_dir / "prompt_prefix.md"
    if prefix.exists():
        parts.append(prefix.read_text())
        parts.append("\n\n---\n\n")

    # Framework base
    parts.append(fw_prompt.read_text())

    # Optional suffix
    suffix = sub_dir / "prompt_suffix.md"
    if suffix.exists():
        parts.append("\n\n---\n\n")
        parts.append(suffix.read_text())

    source = f"layered:{fw_prompt}+{sub_dir}"
    return "".join(parts), source
