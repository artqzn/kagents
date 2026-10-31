"""MM-specific extra tools for the Tester agent."""
from __future__ import annotations
from framework.agents.common.tools.base import Tool, run_cmd


def extra_tools(subsystem) -> list[Tool]:
    """Register MM-specific Tester tools."""
    return [
        Tool(
            name="run_damon_suite",
            description="Run DAMON KUnit tests (mm/damon).",
            fn=_run_damon,
            schema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


def _run_damon(**kwargs) -> dict:
    """Run DAMON KUnit tests."""
    return run_cmd([
        "./tools/testing/kunit/kunit.sh", "run",
        "--arch=arm64", "--make_options=LLVM=1",
        "damon",
    ], timeout=300)
