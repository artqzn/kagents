"""Common tools package — also contains the layered tool resolver.

This package contains:
- Tool base class and helpers (base.py)
- Individual tool modules (filesystem.py, gitops.py, build.py, etc.)
- resolve_tools() function for layered tool resolution

Note: resolve_tools() is defined here (in __init__.py) rather than in a
separate tools.py file because Python package directories take import
precedence over sibling .py modules. Having both tools/ (dir) and
tools.py (file) in the same parent causes the package to shadow the module.
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
    """Load tools for a role.

    If the role has a tools/__init__.py with a tools() function, use that
    (it selects which common tools to include). Otherwise, load all common
    tool modules directly.
    """
    # Check if the role has a tools package with a tools() function
    role_tools_init = FRAMEWORK / "agents" / role / "tools" / "__init__.py"
    if role_tools_init.exists():
        mod = _load_module(
            role_tools_init,
            f"framework.agents.{role}.tools",
        )
        if hasattr(mod, "tools"):
            return mod.tools(subsystem)

    # Fallback: load all common tool modules directly
    tools = []
    common_dir = FRAMEWORK / "agents" / "common" / "tools"
    if common_dir.exists():
        for py_file in sorted(common_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            mod = _load_module(
                py_file,
                f"framework.agents.common.tools.{py_file.stem}",
            )
            if hasattr(mod, "tools"):
                tools.extend(mod.tools(subsystem))

    return tools


def resolve_tools(role: str, subsystem) -> list[Tool]:
    """Resolve tool list for (subsystem, role).

    Resolution order:
    1. Full override: subsystems/<name>/agents/<role>/tools.py
    2. Layered: framework/agents/<role>/tools/*.py + subsystem extra_tools.py
    3. Default: framework/agents/<role>/tools/*.py (framework common tools)

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
