"""Tester tools — build, qemu, kunit, ltp."""
from __future__ import annotations
from framework.agents.common.tools.build import tools as build_tools
from framework.agents.common.tools.qemu import tools as qemu_tools
from framework.agents.common.tools.kunit import tools as kunit_tools
from framework.agents.common.tools.ltp import tools as ltp_tools


def tools(subsystem) -> list:
    """Tester gets: build, qemu, kunit, ltp tools."""
    all_tools = []
    all_tools.extend(build_tools(subsystem))
    all_tools.extend(qemu_tools(subsystem))
    all_tools.extend(kunit_tools(subsystem))
    all_tools.extend(ltp_tools(subsystem))
    return all_tools
