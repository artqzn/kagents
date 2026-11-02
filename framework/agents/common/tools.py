"""Layered tool resolution.

For any (subsystem, role) combination, resolves the tool list via:

1. Full override: subsystems/<name>/agents/<role>/tools.py
2. Layered: framework/agents/<role>/tools/*.py + subsystem extra_tools.py
3. Default: framework/agents/<role>/tools/*.py
"""
from __future__ import annotations
from pathlib import Path
import importlib.util
import sys

from framework.agents.common.tools.base import Tool
from framework.agents.common.prompt import FRAMEWORK, SUBSYSTEMS


def _load_module(path: Path, mod_name: str):
    """Load a Python module from a file path."""
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_framework_tools(role: str, subsystem) -> list[Tool]:
    """Load all tools from framework/agents/<role>/tools/*.py."""
    tools_dir = FRAMEWORK / "agents" / role / "tools"
    if not tools_dir.exists():
        # Fallback: use common tools
        tools_dir = FRAMEWORK / "agents" / "common" / "tools"

    if not tools_dir.exists():
        return []

    tools = []
    # Always include common tools
    common_dir = FRAMEWORK / "agents" / "common" / "tools"
    if common_dir.exists() and common_dir != tools_dir:
        for py_file in sorted(common_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            mod = _load_module(
                py_file,
                f"framework.agents.common.tools.{py_file.stem}",
            )
            if hasattr(mod, "tools"):
                tools.extend(mod.tools(subsystem))

    # Role-specific tools (if the dir exists and is different from common)
    if tools_dir != common_dir and tools_dir.exists():
        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            mod = _load_module(
                py_file,
                f"framework.agents.{role}.tools.{py_file.stem}",
            )
            if hasattr(mod, "tools"):
                tools.extend(mod.tools(subsystem))

    return tools


def resolve_tools(role: str, subsystem) -> list[Tool]:
    """Resolve tool list for (subsystem, role).

    Returns a list of Tool instances.
    """
    sub_dir = SUBSYSTEMS / subsystem.name / "agents" / role

    # Level 1: full override
    full_override = sub_dir / "tools.py"
    if full_override.exists():
        mod = _load_module(
            full_override,
            f"subsystems.{subsystem.name}.agents.{role}.tools",
        )
        if hasattr(mod, "tools"):
            return mod.tools(subsystem)

    # Level 2: framework + extras
    base_tools = _load_framework_tools(role, subsystem)

    extras_path = sub_dir / "extra_tools.py"
    if extras_path.exists():
        mod = _load_module(
            extras_path,
            f"subsystems.{subsystem.name}.agents.{role}.extra_tools",
        )
        if hasattr(mod, "extra_tools"):
            base_tools.extend(mod.extra_tools(subsystem))

    return base_tools
