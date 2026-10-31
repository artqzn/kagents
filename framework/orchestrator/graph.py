"""LangGraph state machine for the agent pipeline.

State machine:
    PLAN -> IMPL -> REVIEW -> TEST -> MERGED
                   |              |
                   v              |
                 (blocked)      (fail)
                   |              |
                   v              v
                  IMPL <-------- IMPL (retry with feedback)

After max_iterations (default 3), a failing task is marked FAILED.
All edges are conditional — if any phase fails, the pipeline stops.
"""
from __future__ import annotations
from typing import TypedDict, Optional, Any
import sys
import json
import time

try:
    from langgraph.graph import StateGraph, END
except ImportError:
    StateGraph = None
    END = "END"

from framework.orchestrator.schemas import Ticket


# ANSI colors
_R = '\033[0m'
_BOLD = '\033[1m'
_RED = '\033[31m'
_GREEN = '\033[32m'
_YELLOW = '\033[33m'
_BLUE = '\033[34m'
_CYAN = '\033[36m'
_GRAY = '\033[90m'
_BG_GREEN = '\033[42m'
_BG_RED = '\033[41m'
_BG_BLUE = '\033[44m'


class OrchestratorState(TypedDict):
    ticket: Ticket
    last_error: Optional[str]


def _c(color: str, text: str) -> str:
    if sys.stderr.isatty():
        return f"{color}{text}{_R}"
    return text


def _log(msg: str):
    print(msg, file=sys.stderr, flush=True)


def _phase_banner(phase: str, task_id: str, title: str, iteration: int = 0,
                  feedback: str = None):
    """Print a colorful phase banner."""
    colors = {
        "PLAN": _BG_BLUE,
        "IMPLEMENT": _BLUE,
        "REVIEW": _MAGENTA if False else '\033[35m',
        "TEST": _YELLOW,
    }
    color = colors.get(phase, _CYAN)
    line = '═' * 70
    _log(f"\n{_BOLD}{color}{line}{_R}")
    _log(f"{_BOLD}{color}  ▶  PHASE: {phase}{_R}")
    _log(f"{color}     Task: {task_id} — {title}{_R}")
    if iteration > 0:
        _log(f"{color}     Iteration: {iteration} (retry with feedback){_R}")
    if feedback:
        _log(f"{color}     Feedback:{_R}")
        for line in feedback.split('\n')[:5]:
            _log(f"{_GRAY}       {line}{_R}")
    _log(f"{_BOLD}{color}{line}{_R}")


def _result_box(success: bool, title: str, details: list[str] = None):
    """Print a result box (green for success, red for failure)."""
    color = _GREEN if success else _RED
    icon = '✓' if success else '✗'
    line = '─' * 70
    _log(f"\n{color}{line}{_R}")
    _log(f"{_BOLD}{color}  {icon}  {title}{_R}")
    if details:
        for d in details:
            _log(f"{color}     {d}{_R}")
    _log(f"{color}{line}{_R}")


def _format_review_feedback(blockers: list[dict]) -> str:
    lines = ["REVIEW BLOCKERS (must fix):"]
    for b in blockers:
        lines.append(
            f"  - {b.get('file', '?')}:{b.get('line', '?')} — {b.get('comment', '')}"
        )
        if b.get("suggested_fix"):
            lines.append(f"    Suggested: {b['suggested_fix']}")
    return "\n".join(lines)


def _format_test_feedback(failures: list[dict]) -> str:
    lines = ["TEST FAILURES (must fix):"]
    for f in failures:
        lines.append(
            f"  - Tier {f.get('tier', '?')}: {f.get('detail', f.get('status', ''))}"
        )
        if f.get("log_path"):
            lines.append(f"    Log: {f['log_path']}")
    return "\n".join(lines)


def _run_agent_stub(role: str, ticket: Ticket) -> dict:
    return {"ok": True, "status": "ok", "comments": [], "results": []}


def _entry_node(state: OrchestratorState) -> OrchestratorState:
    """Entry node — routes to the correct phase based on ticket status.

    If the ticket is being resumed (status is impl/review/test), skip
    plan and go directly to the appropriate phase.
    """
    t = state["ticket"]
    _log(f"  {_c(_GRAY, f'Entry: status={t.status}, iteration={t.iteration}')}")
    # Don't change anything — just let route() handle it
    return state


def make_plan_node(run_agent_fn):
    def plan_node(state: OrchestratorState) -> OrchestratorState:
        t = state["ticket"]

        # If resuming and plan already done (has rfc_doc + branch), skip
        if t.rfc_doc and t.branch_impl:
            _log(f"\n  {_c(_GRAY, '⏭️  Skipping PLAN (already completed)')}")
            t.status = "impl"
            return state

        _phase_banner("PLAN", t.task_id, t.title)
        start = time.time()
        out = run_agent_fn("planner", t)
        elapsed = time.time() - start

        if out.get("ok"):
            t.rfc_doc = out.get("rfc_doc")
            t.branch_impl = out.get("branch") or out.get("branch_impl")
            t.worktree_path = out.get("worktree_path")

            if not t.worktree_path and t.branch_impl:
                import os
                workspace = os.environ.get("KAG_WORKSPACE", "/workspace")
                t.worktree_path = f"{workspace}/branches/impl/{t.branch_impl}"
                _log(f"  {_c(_YELLOW, '⚠ worktree_path not in result — derived:')} "
                     f"{_c(_GRAY, t.worktree_path)}")

            t.status = "impl"
            _result_box(True, f"PLAN COMPLETE [{_format_duration(elapsed)}]", [
                f"RFC:       {t.rfc_doc or '(missing)'}",
                f"Branch:    {t.branch_impl or '(missing)'}",
                f"Worktree:  {t.worktree_path or '(missing)'}",
            ])
        else:
            t.status = "failed"
            state["last_error"] = out.get("error", "Planner failed")
            _result_box(False, f"PLAN FAILED [{_format_duration(elapsed)}]", [
                f"Error: {state['last_error']}",
            ])
        return state
    return plan_node


def make_impl_node(run_agent_fn):
    def impl_node(state: OrchestratorState) -> OrchestratorState:
        t = state["ticket"]

        # If resuming and impl already done (has commits), skip
        if t.status == "review" or t.status == "test":
            _log(f"\n  {_c(_GRAY, '⏭️  Skipping IMPLEMENT (already completed)')}")
            return state

        _phase_banner("IMPLEMENT", t.task_id, t.title, t.iteration,
                       t.iteration_feedback)
        start = time.time()
        out = run_agent_fn("implementer", t)
        elapsed = time.time() - start

        if out.get("ok"):
            t.status = "review"
            t.iteration_feedback = None
            commits = out.get("commits", [])
            _result_box(True, f"IMPLEMENTATION COMPLETE [{_format_duration(elapsed)}]", [
                f"Commits: {len(commits)}",
            ] + [f"  {c[:12]}" for c in commits[:5]])
        else:
            t.status = "failed"
            state["last_error"] = out.get("error", "Implementer failed")
            _result_box(False, f"IMPLEMENTATION FAILED [{_format_duration(elapsed)}]", [
                f"Error: {state['last_error']}",
            ])
        return state
    return impl_node


def make_review_node(run_agent_fn):
    def review_node(state: OrchestratorState) -> OrchestratorState:
        t = state["ticket"]
        _phase_banner("REVIEW", t.task_id, t.title)
        start = time.time()
        out = run_agent_fn("reviewer", t)
        elapsed = time.time() - start
        t.review_comments = out.get("comments", [])

        status = out.get("status", "failed")
        if status == "failed":
            t.status = "failed"
            state["last_error"] = out.get("error", "Reviewer crashed")
            _result_box(False, f"REVIEW FAILED [{_format_duration(elapsed)}]", [
                f"Error: {state['last_error']}",
            ])
        elif status == "blocked":
            blockers = [c for c in t.review_comments
                        if c.get("severity") == "blocker"]
            t.iteration_feedback = _format_review_feedback(blockers)
            t.iteration += 1
            if t.iteration >= t.max_iterations:
                t.status = "failed"
                state["last_error"] = "Max review iterations exceeded"
                _result_box(False, f"REVIEW BLOCKED — MAX ITERATIONS [{_format_duration(elapsed)}]", [
                    f"{len(blockers)} blockers found, {t.iteration} iterations used",
                ])
            else:
                t.status = "impl"
                _result_box(False, f"REVIEW BLOCKED — BACK TO IMPLEMENTATION [{_format_duration(elapsed)}]", [
                    f"{len(blockers)} blockers found (iteration {t.iteration}/{t.max_iterations})",
                ])
        else:
            t.status = "test"
            minors = [c for c in t.review_comments
                      if c.get("severity") in ("minor", "nit")]
            _result_box(True, f"REVIEW PASSED [{_format_duration(elapsed)}]", [
                f"Comments: {len(t.review_comments)} total, {len(minors)} minor/nit",
            ])
        return state
    return review_node


def make_test_node(run_agent_fn):
    def test_node(state: OrchestratorState) -> OrchestratorState:
        t = state["ticket"]
        _phase_banner("TEST", t.task_id, t.title)
        start = time.time()
        out = run_agent_fn("tester", t)
        elapsed = time.time() - start
        t.test_results = out.get("results", [])

        status = out.get("status", "failed")
        if status == "failed":
            t.status = "failed"
            state["last_error"] = out.get("error", "Tester crashed")
            _result_box(False, f"TEST FAILED [{_format_duration(elapsed)}]", [
                f"Error: {state['last_error']}",
            ])
        elif status == "ok":
            # Check if all non-skipped results passed.
            # Skipped tests (KUnit/LTP not available) are acceptable —
            # they don't count as failures.
            non_skip_results = [
                r for r in t.test_results
                if r.get("status") != "skip"
            ]
            all_passed = all(
                r.get("status") == "pass" for r in non_skip_results
            ) if non_skip_results else True
            if all_passed:
                t.status = "merged"
                passed = sum(1 for r in t.test_results if r.get("status") == "pass")
                skipped = sum(1 for r in t.test_results if r.get("status") == "skip")
                _result_box(True, f"ALL TESTS PASSED — TASK MERGED! [{_format_duration(elapsed)}]", [
                    f"Results: {passed} passed, {skipped} skipped",
                ])
            else:
                failures = [r for r in t.test_results
                            if r.get("status") == "fail"]
                t.iteration_feedback = _format_test_feedback(failures)
                t.iteration += 1
                if t.iteration >= t.max_iterations:
                    t.status = "failed"
                    state["last_error"] = "Max test iterations exceeded"
                    _result_box(False, f"TESTS FAILED — MAX ITERATIONS [{_format_duration(elapsed)}]", [
                        f"{len(failures)} failures, {t.iteration} iterations used",
                    ])
                else:
                    t.status = "impl"
                    _result_box(False, f"TESTS FAILED — BACK TO IMPLEMENTATION [{_format_duration(elapsed)}]", [
                        f"{len(failures)} failures (iteration {t.iteration}/{t.max_iterations})",
                    ])
        else:
            t.status = "failed"
            state["last_error"] = out.get("error", "Unknown tester status")
            _result_box(False, f"TEST FAILED [{_format_duration(elapsed)}]", [
                f"Error: {state['last_error']}",
            ])
        return state
    return test_node


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    elif seconds < 60:
        return f"{seconds:.1f}s"
    else:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s}s"


def route(state: OrchestratorState) -> str:
    s = state["ticket"].status
    if s == "planning":
        return "plan"
    if s == "impl":
        return "impl"
    if s == "review":
        return "review"
    if s == "test":
        return "test"
    return END


def build_graph(run_agent_fn=None):
    if StateGraph is None:
        raise ImportError(
            "langgraph is not installed. Run: pip install langgraph"
        )

    if run_agent_fn is None:
        run_agent_fn = _run_agent_stub

    g = StateGraph(OrchestratorState)

    g.add_node("entry", _entry_node)
    g.add_node("plan", make_plan_node(run_agent_fn))
    g.add_node("impl", make_impl_node(run_agent_fn))
    g.add_node("review", make_review_node(run_agent_fn))
    g.add_node("test", make_test_node(run_agent_fn))

    # Entry node routes based on ticket status
    g.set_entry_point("entry")
    g.add_conditional_edges("entry", route)

    # All other edges are conditional — if any node fails, route() sends to END.
    g.add_conditional_edges("plan", route)
    g.add_conditional_edges("impl", route)
    g.add_conditional_edges("review", route)
    g.add_conditional_edges("test", route)

    return g.compile()
