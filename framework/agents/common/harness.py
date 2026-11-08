"""Common agent harness: read ticket, run LLM loop, write result."""
from __future__ import annotations
import os
import sys
import json
import time
from pathlib import Path
from typing import Optional

# ANSI color codes for terminal output
class C:
    RESET   = '\033[0m'
    BOLD    = '\033[1m'
    DIM     = '\033[2m'
    RED     = '\033[31m'
    GREEN   = '\033[32m'
    YELLOW  = '\033[33m'
    BLUE    = '\033[34m'
    MAGENTA = '\033[35m'
    CYAN    = '\033[36m'
    GRAY    = '\033[90m'
    BG_BLUE = '\033[44m'
    BG_GREEN= '\033[42m'
    BG_RED  = '\033[41m'
    BG_YEL  = '\033[43m'


def _c(color: str, text: str) -> str:
    """Colorize text if stderr is a terminal."""
    if sys.stderr.isatty():
        return f"{color}{text}{C.RESET}"
    return text


def _log(msg: str, color: str = C.RESET):
    """Print a colorized message to stderr."""
    print(_c(color, msg), file=sys.stderr, flush=True)


def _box(title: str, color: str = C.CYAN, width: int = 70):
    """Print a colored box with a title."""
    line = '─' * width
    _log(f"\n{color}{line}{C.RESET}")
    _log(f"{color}  {title}{C.RESET}")
    _log(f"{color}{line}{C.RESET}")


def _separator(color: str = C.GRAY):
    """Print a thin separator line."""
    _log(f"{color}{'·' * 70}{C.RESET}")


def _format_duration(seconds: float) -> str:
    """Format duration in human-readable form."""
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    elif seconds < 60:
        return f"{seconds:.1f}s"
    else:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s}s"


def _truncate(text: str, max_len: int = 500) -> str:
    """Truncate text with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f'... ({len(text)} chars total)'

try:
    from litellm import completion
    HAS_LITELLM = True
except ImportError:
    HAS_LITELLM = False
    completion = None

# Suppress LiteLLM's diagnostic output to stdout — it contaminates JSON.
# LiteLLM prints "Give Feedback / Get Help" and error info to stdout,
# which breaks the orchestrator's JSON parsing.
import os as _os
_os.environ.setdefault("LITELLM_LOG", "ERROR")  # Only log errors, not info

# Redirect litellm's stdout printing to stderr
import io as _io
_original_stdout = sys.stdout

from framework.config import get_llm_config, WORKSPACE
from framework.subsystem import load_subsystem
from framework.agents.common.prompt import resolve_prompt
from framework.agents.common.tools import resolve_tools
from framework.agents.common.cost_tracker import register_cost_tracker


def get_subsystem():
    """Load the active subsystem from env."""
    name = os.environ.get("KAG_SUBSYSTEM", "mm")
    return load_subsystem(name)


def get_role() -> str:
    """Get the agent role from env."""
    return os.environ.get("AGENT_ROLE", "unknown")


def call_llm(role: str, system_prompt: str, messages: list[dict],
             tools: list[dict] = None, metadata: dict = None) -> dict:
    """Provider-agnostic LLM call via LiteLLM with rich logging."""
    if not HAS_LITELLM:
        return {
            "content": '{"ok": false, "error": "litellm not installed"}',
            "tool_calls": None,
        }

    debug = _os.environ.get("KAG_DEBUG", "0") == "1"
    cfg = get_llm_config(role)
    kwargs = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "max_tokens": cfg["max_tokens"],
        "metadata": {"agent": role, **(metadata or {})},
    }
    if tools:
        kwargs["tools"] = tools

    # Log the request (only in debug mode)
    if debug:
        msg_count = len(messages)
        _log(f"  {_c(C.GRAY, '→')} {_c(C.BLUE, 'LLM Request')} "
             f"{_c(C.GRAY, f'[{cfg["model"]} | {msg_count} messages | max {cfg["max_tokens"]} tokens]')}",
             )

    old_stdout = sys.stdout
    sys.stdout = sys.stderr

    max_retries = 3
    response = None
    last_error = None
    for attempt in range(max_retries):
        try:
            start = time.time()
            response = completion(**kwargs)
            elapsed = time.time() - start
            if debug:
                _log(f"  {_c(C.GRAY, '←')} {_c(C.GREEN, 'LLM Response')} "
                     f"{_c(C.GRAY, f'[{_format_duration(elapsed)}]')}")
            break
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            is_transient = any(s in error_str for s in [
                "no address associated", "timeout", "timed out",
                "connection", "temporary", "503", "502", "500",
                "internal server error", "rate limit",
            ])
            if is_transient and attempt < max_retries - 1:
                wait = (attempt + 1) * 5
                _log(f"  {_c(C.YELLOW, '⚠')} {_c(C.YELLOW, f'Transient error (attempt {attempt+1}/{max_retries}), retrying in {wait}s...')}")
                _log(f"      {_c(C.GRAY, str(e)[:200])}")
                time.sleep(wait)
                continue

            if cfg["fallback"]:
                _log(f"  {_c(C.YELLOW, '⚠')} {_c(C.YELLOW, f'Trying fallback model: {cfg["fallback"]}')}")
                kwargs["model"] = cfg["fallback"]
                try:
                    start = time.time()
                    response = completion(**kwargs)
                    elapsed = time.time() - start
                    if debug:
                        _log(f"  {_c(C.GRAY, '←')} {_c(C.GREEN, 'LLM Response (fallback)')} "
                             f"{_c(C.GRAY, f'[{_format_duration(elapsed)}]')}")
                    break
                except Exception as e2:
                    sys.stdout = old_stdout
                    _log(f"  {_c(C.RED, '✗')} {_c(C.RED, f'Both models failed: {str(e2)[:200]}')}")
                    return {
                        "content": json.dumps({
                            "ok": False,
                            "error": f"Both primary and fallback failed: {e2}"
                        }),
                        "tool_calls": None,
                    }
            else:
                sys.stdout = old_stdout
                _log(f"  {_c(C.RED, '✗')} {_c(C.RED, f'LLM call failed: {str(e)[:200]}')}")
                return {
                    "content": json.dumps({
                        "ok": False,
                        "error": f"LLM call failed after {max_retries} retries: {last_error}"
                    }),
                    "tool_calls": None,
                }

    sys.stdout = old_stdout

    if response is None:
        _log(f"  {_c(C.RED, '✗')} {_c(C.RED, f'No response: {last_error}')}")
        return {
            "content": json.dumps({
                "ok": False,
                "error": f"LLM call failed: {last_error}"
            }),
            "tool_calls": None,
        }

    msg = response.choices[0].message

    # Log reasoning content if present
    reasoning = getattr(msg, "reasoning_content", None)
    if reasoning:
        # Per-task artifacts: artifacts/<task-id>/<role>/
        task_id = (metadata or {}).get("task_id", "unknown")
        reasoning_dir = WORKSPACE / "artifacts" / task_id / role
        reasoning_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        (reasoning_dir / f"reasoning-{ts}.md").write_text(reasoning)
        if debug:
            _log(f"  {_c(C.MAGENTA, '🧠')} {_c(C.MAGENTA, f'Reasoning logged ({len(reasoning)} chars)')}")

    # Log token usage (only in debug mode)
    if debug and hasattr(response, 'usage') and response.usage:
        pt = response.usage.prompt_tokens
        ct = response.usage.completion_tokens
        _log(f"  {_c(C.GRAY, f'   Tokens: {pt} prompt + {ct} completion = {pt+ct} total')}")

    return {
        "content": msg.content,
        "tool_calls": msg.tool_calls,
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens if hasattr(response, 'usage') else 0,
            "completion_tokens": response.usage.completion_tokens if hasattr(response, 'usage') else 0,
        },
    }


def read_ticket() -> dict:
    """Read JSON ticket from stdin."""
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("No ticket on stdin")
    return json.loads(raw)


def write_result(result: dict):
    """Write JSON result to stdout.

    CRITICAL: Only valid JSON goes to stdout. All diagnostic output
    (LLM errors, LiteLLM messages) goes to stderr. The orchestrator
    parses stdout as JSON — any non-JSON output breaks it.
    """
    try:
        sys.stdout.write(json.dumps(result, default=str))
        sys.stdout.flush()
    except Exception as e:
        # If the result itself can't be serialized, output a minimal error
        sys.stdout.write(json.dumps({
            "ok": False,
            "status": "failed",
            "error": f"Failed to serialize result: {e}",
        }))
        sys.stdout.flush()


def _agent_plan(role: str, ticket: dict) -> list[str]:
    """Generate a human-readable plan for what the agent will do."""
    plans = {
        "planner": [
            "Read and analyze the task description",
            f"Explore {ticket.get('subsystem', 'mm')} subsystem source files",
            "Write an RFC document to docs/rfcs/<task_id>.md",
            "Create a git worktree branch for implementation",
            "Return RFC path, branch name, and worktree path",
        ],
        "implementer": [
            "Read the RFC document for implementation guidance",
            "Edit source files in the worktree using edit_file",
            "Verify each file compiles using make_file",
            "Commit changes using git_commit with specific files",
            f"Build kernel Image using make_image (ccache enabled)",
            "Return branch name and commit SHAs",
        ],
        "reviewer": [
            f"Get the diff from branch {ticket.get('branch_impl', '?')}",
            "Run checkpatch.pl on the patch for style violations",
            "Run sparse on affected source files for type errors",
            "Analyze the diff for bugs, locking issues, and design flaws",
            "Produce structured review comments (blocker/major/minor/nit)",
            "Return review status (ok/blocked/failed) and comments",
        ],
        "tester": [
            f"Build kernel Image from {ticket.get('worktree_path', 'worktree')}",
            "Boot the kernel in QEMU and verify it reaches initramfs",
            f"Run KUnit tests for {ticket.get('subsystem', 'mm')} subsystem",
            f"Run LTP tests for {ticket.get('subsystem', 'mm')} subsystem",
            "Collect and report all test results (pass/fail/skip)",
        ],
    }
    return plans.get(role, ["Execute the assigned task"])


def _step_report(tool_name: str, args: dict, result: dict,
                 reasoning: str, elapsed: float) -> list[str]:
    """Generate a concise one-line step report for a single tool call."""
    lines = []
    ok = isinstance(result, dict) and result.get("ok", True)
    icon = "✅" if ok else "❌"
    status = "PASSED" if ok else "FAILED"

    # Build a concise one-line action summary
    action_desc = ""
    if tool_name == "read_file":
        action_desc = f"Read '{args.get('path', '?')}'"
    elif tool_name == "edit_file":
        action_desc = f"Edited '{args.get('path', '?')}'"
    elif tool_name == "write_file":
        action_desc = f"Wrote {len(args.get('content', ''))} chars to '{args.get('path', '?')}'"
    elif tool_name == "write_doc":
        action_desc = f"Wrote RFC '{args.get('filename', '?')}'"
    elif tool_name == "create_branch":
        action_desc = f"Created worktree '{args.get('name', '?')}'"
    elif tool_name == "git_commit":
        files = args.get('files', [])
        action_desc = f"Committed {len(files)} file(s): {', '.join(files[:3])}"
    elif tool_name == "make_image":
        action_desc = "Full kernel build (make Image)"
    elif tool_name == "make_file":
        action_desc = f"Compiled '{args.get('filepath', '?')}'"
    elif tool_name == "make_defconfig":
        action_desc = "Generated defconfig"
    elif tool_name == "boot_qemu":
        action_desc = "Booted kernel in QEMU"
    elif tool_name == "run_kunit":
        action_desc = f"Ran KUnit (filter: {args.get('filter', 'default')})"
    elif tool_name == "run_ltp":
        action_desc = f"Ran LTP (category: {args.get('category', 'default')})"
    elif tool_name == "run_checkpatch_diff":
        action_desc = "Ran checkpatch.pl on diff"
    elif tool_name == "run_sparse":
        action_desc = f"Ran sparse on '{args.get('file', '?')}'"
    elif tool_name == "run_smatch":
        action_desc = f"Ran smatch on '{args.get('file', '?')}'"
    elif tool_name == "git_log":
        action_desc = f"Retrieved {args.get('n', 20)} git log entries"
    elif tool_name == "git_diff":
        action_desc = "Retrieved git diff"
    elif tool_name == "git_changed_files":
        action_desc = "Listed changed files"
    elif tool_name == "list_files":
        action_desc = f"Listed files in '{args.get('path', '.')}'"
    elif tool_name == "remove_git_lock":
        action_desc = "Removed stale git lock"
    elif tool_name == "finish":
        action_desc = "Agent signaled completion"
    else:
        action_desc = f"Called {tool_name}"

    # One-line summary
    lines.append(f"  {icon} {tool_name} [{_format_duration(elapsed)}] — {action_desc}")

    # Only show error if failed
    if not ok and isinstance(result, dict) and result.get("error"):
        lines.append(f"     {_c(C.RED, 'Error:')} {result['error'][:150]}")

    return lines


def _review_report(role: str, result: dict) -> list[str]:
    """Generate a detailed review report for the reviewer agent."""
    if role != "reviewer":
        return []

    comments = result.get("comments", [])
    if not comments:
        return ["  No review comments produced."]

    lines = []
    blockers = [c for c in comments if c.get("severity") == "blocker"]
    majors = [c for c in comments if c.get("severity") == "major"]
    minors = [c for c in comments if c.get("severity") in ("minor", "nit")]

    if blockers:
        lines.append(f"\n  {_c(C.RED, C.BOLD + '🔴 BLOCKERS (' + str(len(blockers)) + '):')}")
        for b in blockers:
            lines.append(f"     {_c(C.RED, f'{b.get('file','?')}:{b.get('line','?')}')} — {b.get('comment','')[:120]}")
            if b.get("suggested_fix"):
                fix = b["suggested_fix"][:120]
                lines.append(f"     {_c(C.GRAY, f'   Fix: {fix}')}")

    if majors:
        lines.append(f"\n  {_c(C.YELLOW, C.BOLD + '🟡 MAJOR ISSUES (' + str(len(majors)) + '):')}")
        for m in majors:
            lines.append(f"     {_c(C.YELLOW, f'{m.get('file','?')}:{m.get('line','?')}')} — {m.get('comment','')[:120]}")
            if m.get("suggested_fix"):
                fix = m["suggested_fix"][:120]
                lines.append(f"     {_c(C.GRAY, f'   Fix: {fix}')}")

    if minors:
        lines.append(f"\n  {_c(C.GRAY, '🔹 MINOR/NIT (' + str(len(minors)) + '):')}")
        for m in minors:
            lines.append(f"     {_c(C.GRAY, f'{m.get('file','?')}:{m.get('line','?')}')} — {m.get('comment','')[:100]}")

    return lines


def _agent_summary(role: str, result: dict, duration: float,
                   tokens: int, tools_used: list[str]) -> list[str]:
    """Generate a human-readable summary of what the agent accomplished."""
    ok = result.get("ok", False) or result.get("status") in ("ok", "merged")
    lines = []

    if role == "planner":
        if ok:
            lines.append(f"✅ RFC written: {result.get('rfc_doc', '?')}")
            lines.append(f"✅ Branch created: {result.get('branch', '?')}")
            lines.append(f"✅ Worktree: {result.get('worktree_path', '?')}")
        else:
            lines.append(f"❌ Failed: {result.get('error', 'unknown')[:200]}")

    elif role == "implementer":
        if ok:
            commits = result.get("commits", [])
            lines.append(f"✅ {len(commits)} commit(s) created:")
            for c in commits[:5]:
                lines.append(f"   {c[:12]}")
            lines.append(f"✅ Branch: {result.get('branch', '?')}")
        else:
            lines.append(f"❌ Failed: {result.get('error', 'unknown')[:200]}")

    elif role == "reviewer":
        status = result.get("status", "failed")
        comments = result.get("comments", [])
        blockers = [c for c in comments if c.get("severity") == "blocker"]
        majors = [c for c in comments if c.get("severity") == "major"]
        minors = [c for c in comments if c.get("severity") in ("minor", "nit")]
        if status == "ok":
            lines.append(f"✅ Review passed — no blockers found")
            lines.append(f"   {len(comments)} comment(s): {len(majors)} major, {len(minors)} minor/nit")
        elif status == "blocked":
            lines.append(f"⚠️ Review blocked — {len(blockers)} blocker(s) found")
            for b in blockers[:3]:
                lines.append(f"   🔴 {b.get('file','?')}:{b.get('line','?')} — {b.get('comment','')[:80]}")
            lines.append(f"   Also: {len(majors)} major, {len(minors)} minor/nit")
        else:
            lines.append(f"❌ Review failed: {result.get('error', 'unknown')[:200]}")

    elif role == "tester":
        status = result.get("status", "failed")
        results = result.get("results", [])
        passed = [r for r in results if r.get("status") == "pass"]
        failed = [r for r in results if r.get("status") == "fail"]
        skipped = [r for r in results if r.get("status") == "skip"]
        if status == "ok":
            lines.append(f"✅ All tests passed!")
            lines.append(f"   {len(passed)} passed, {len(skipped)} skipped, {len(failed)} failed")
            for r in results:
                icon = "✅" if r.get("status") == "pass" else "⏭️" if r.get("status") == "skip" else "❌"
                lines.append(f"   {icon} {r.get('tier', '?')}: {r.get('status', '?')} — {r.get('detail', '')[:60]}")
        else:
            lines.append(f"❌ Tests failed: {result.get('error', 'unknown')[:200]}")
            for r in results:
                icon = "✅" if r.get("status") == "pass" else "⏭️" if r.get("status") == "skip" else "❌"
                lines.append(f"   {icon} {r.get('tier', '?')}: {r.get('status', '?')} — {r.get('detail', '')[:60]}")

    lines.append(f"")
    lines.append(f"📊 Stats: {_format_duration(duration)} | {tokens:,} tokens | {len(tools_used)} tool calls")

    return lines


def run_agent_loop(role: str, ticket: dict, initial_message: str) -> dict:
    """Standard LLM tool-call loop with rich colorized output.

    Logs every step:
    - Agent plan at the start (what it will do)
    - Each iteration with LLM thinking and tool calls
    - Agent summary at the end (what it accomplished)
    """
    import os as _os
    debug = _os.environ.get("KAG_DEBUG", "1") == "1"

    subsystem = get_subsystem()
    system_prompt, _ = resolve_prompt(role, subsystem.name)
    tools = resolve_tools(role, subsystem)

    register_cost_tracker()

    finish_tool = _FinishTool()
    tools_with_finish = list(tools) + [finish_tool]

    messages = [{"role": "user", "content": initial_message}]
    tool_schemas = [t.to_openai_schema() for t in tools_with_finish]
    tool_map = {t.name: t for t in tools_with_finish}

    # ─── Agent header ──────────────────────────────────────────────
    _box(f"AGENT: {role.upper()} — {ticket.get('task_id', '?')}", C.CYAN)
    _log(f"  {_c(C.GRAY, 'Subsystem:')} {subsystem.display_name}")
    _log(f"  {_c(C.GRAY, 'Tools:')} {len(tools)} available (+ finish)")
    _log(f"  {_c(C.GRAY, 'Model:')} {get_llm_config(role)['model']}")

    # ─── Agent plan ────────────────────────────────────────────────
    _log(f"\n  {_c(C.BOLD, '📋 PLAN:')}")
    for i, step in enumerate(_agent_plan(role, ticket), 1):
        _log(f"  {_c(C.CYAN, f'  {i}.')} {_c(C.RESET, step)}")
    _separator()

    max_iterations = 30
    total_tokens = 0
    loop_start = time.time()
    tools_used = []
    step_reports = []  # Collect all step reports for final execution report

    for iteration in range(max_iterations):
        iter_start = time.time()
        if debug:
            _log(f"\n  {_c(C.BOLD, f'━━━ Iteration {iteration + 1}/{max_iterations} ━━━')}",
                 C.YELLOW)

        response = call_llm(
            role, system_prompt, messages, tool_schemas,
            metadata={"task_id": ticket.get("task_id")},
        )

        # Track token usage
        if response.get("usage"):
            total_tokens += response["usage"].get("prompt_tokens", 0)
            total_tokens += response["usage"].get("completion_tokens", 0)

        tool_calls = response.get("tool_calls")

        # Capture LLM reasoning (content before tool calls)
        reasoning = ""
        if debug:
            content = response.get("content", "") or ""
            if content:
                reasoning = content
                _log(f"  {_c(C.BLUE, '💭 Thinking:')}")
                for line in _truncate(content, 500).split('\n'):
                    _log(f"     {_c(C.GRAY, line)}")

        # If no tool calls, LLM produced final answer
        if not tool_calls:
            if debug:
                _log(f"\n  {_c(C.BLUE, '📋 No tool calls — parsing final answer...')}")
            try:
                result = json.loads(response["content"])
                iter_elapsed = time.time() - iter_start
                _log(f"  {_c(C.GREEN, '✓ Parsed JSON result')} "
                     f"{_c(C.GRAY, f'[{_format_duration(iter_elapsed)}]')}")

                # ─── Execution report (concise) ─────────────────────
                _log(f"\n  {_c(C.BOLD, '📋 EXECUTION REPORT:')}")
                _log(f"  {_c(C.GRAY, f'Total steps: {len(step_reports)}')}")
                for s_tool, s_args, s_result, s_reasoning, s_elapsed in step_reports:
                    for line in _step_report(s_tool, s_args, s_result, s_reasoning, s_elapsed):
                        _log(f"  {line}")

                # ─── Review report (reviewer only) ──────────────────
                review_lines = _review_report(role, result)
                if review_lines:
                    _log(f"\n  {_c(C.BOLD, '🔍 REVIEW REPORT:')}")
                    for line in review_lines:
                        _log(f"  {line}")

                # ─── Agent summary ────────────────────────────────
                _log(f"\n  {_c(C.BOLD, '📝 SUMMARY:')}")
                elapsed = time.time() - loop_start
                for line in _agent_summary(role, result, elapsed, total_tokens, [s[0] for s in step_reports]):
                    _log(f"  {line}")
                return result
            except (json.JSONDecodeError, KeyError):
                _log(f"  {_c(C.YELLOW, '⚠ Not valid JSON — nudging to use finish tool...')}")
                messages.append({
                    "role": "assistant",
                    "content": response.get("content"),
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "Your response was not valid JSON. "
                        "You MUST call the `finish` tool to complete. "
                        "Call finish now with your result as a JSON object argument. "
                        f"If you have completed your task, call: "
                        f'finish({{"ok": true, "rfc_doc": "<path>", "branch": "<name>", "worktree_path": "<path>"}}) '
                        f"If you failed, call: "
                        f'finish({{"ok": false, "error": "<reason>"}})'
                    ),
                })
                continue

        # Print and execute tool calls (only in debug mode)
        if debug:
            _log(f"  {_c(C.CYAN, f'🔧 {len(tool_calls)} tool call(s):')}")
            for tc in tool_calls:
                args_str = tc.function.arguments[:300] if tc.function.arguments else "{}"
                _log(f"     {_c(C.CYAN, '→')} {_c(C.BOLD, tc.function.name)}"
                     f" {_c(C.GRAY, args_str)}")

        messages.append({
            "role": "assistant",
            "content": response.get("content"),
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            tool_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}

            # Check if the LLM called finish()
            if tool_name == "finish":
                iter_elapsed = time.time() - iter_start

                # ─── Execution report (concise) ─────────────────────
                _log(f"\n  {_c(C.BOLD, '📋 EXECUTION REPORT:')}")
                _log(f"  {_c(C.GRAY, f'Total steps: {len(step_reports)}')}")
                for s_tool, s_args, s_result, s_reasoning, s_elapsed in step_reports:
                    for line in _step_report(s_tool, s_args, s_result, s_reasoning, s_elapsed):
                        _log(f"  {line}")

                # ─── Review report (reviewer only) ──────────────────
                review_lines = _review_report(role, args)
                if review_lines:
                    _log(f"\n  {_c(C.BOLD, '🔍 REVIEW REPORT:')}")
                    for line in review_lines:
                        _log(f"  {line}")

                # ─── Agent summary ────────────────────────────────
                elapsed = time.time() - loop_start
                _log(f"\n  {_c(C.BOLD, '📝 SUMMARY:')}")
                for line in _agent_summary(role, args, elapsed, total_tokens, [s[0] for s in step_reports]):
                    _log(f"  {line}")
                if isinstance(args, dict):
                    return args
                return {"ok": True, "result": str(args)}

            if tool_name not in tool_map:
                tool_result = {"error": f"Unknown tool: {tool_name}"}
                if debug:
                    _log(f"     {_c(C.RED, '✗')} {_c(C.RED, f'Unknown tool: {tool_name}')}")
            else:
                tool_start = time.time()
                tool_result = tool_map[tool_name](**args)
                tool_elapsed = time.time() - tool_start
                if debug:
                    _log(f"     {_c(C.GRAY, '← ' + _format_duration(tool_elapsed))}", )
                tools_used.append(tool_name)

            # Print step report for this tool call (always shown)
            for line in _step_report(tool_name, args, tool_result, reasoning, tool_elapsed):
                _log(f"  {line}")

            # Collect for final execution report
            step_reports.append((tool_name, args, tool_result, reasoning, tool_elapsed))

            # Print raw tool result (only in debug mode)
            result_str = json.dumps(tool_result, default=str)
            if len(result_str) > 3000:
                result_str_truncated = result_str[:3000] + \
                    f'... (truncated, {len(result_str)} chars total)'
            else:
                result_str_truncated = result_str

            if debug:
                ok = isinstance(tool_result, dict) and tool_result.get("ok", True)
                if ok:
                    _log(f"     {_c(C.GREEN, '✓')} {_c(C.GRAY, _truncate(result_str, 200))}")
                else:
                    _log(f"     {_c(C.YELLOW, '!')} {_c(C.YELLOW, _truncate(result_str, 200))}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": tool_name,
                "content": result_str_truncated,
            })

        # After 15 iterations, add a nudge to finish
        if iteration == 14:
            if debug:
                _log(f"\n  {_c(C.YELLOW, '⚠ 15 iterations used — nudging to finish...')}")
            messages.append({
                "role": "user",
                "content": (
                    "IMPORTANT: You have used 15 of 30 iterations. "
                    "Please call the `finish` tool now with your result. "
                    "If you cannot complete the task, call finish with "
                    "{\"ok\": false, \"error\": \"...\"}."
                ),
            })

    # ─── Max iterations reached — execution report + summary ────────
    _log(f"\n  {_c(C.BOLD, '📋 EXECUTION REPORT:')}")
    _log(f"  {_c(C.GRAY, f'Total steps: {len(step_reports)}')}")
    for s_tool, s_args, s_result, s_reasoning, s_elapsed in step_reports:
        for line in _step_report(s_tool, s_args, s_result, s_reasoning, s_elapsed):
            _log(f"  {line}")

    elapsed = time.time() - loop_start
    _log(f"\n  {_c(C.BOLD, '📝 SUMMARY:')}")
    for line in _agent_summary(role, {"ok": False, "error": f"Exceeded {max_iterations} iterations"},
                                elapsed, total_tokens, [s[0] for s in step_reports]):
        _log(f"  {line}")
    return {
        "ok": False,
        "error": f"Agent exceeded max iterations ({max_iterations})",
    }


class _FinishTool:
    """Special tool that signals agent completion.

    When the LLM calls `finish`, the harness returns the tool's argument
    as the agent's result. This is more reliable than expecting the LLM
    to produce a no-tool-call response with valid JSON.
    """

    name = "finish"
    description = (
        "Call this tool when you have completed your task. Pass the final "
        "result as the argument — it must be a JSON object matching your "
        "role's exit schema. Example: finish({\"ok\": true, \"rfc_doc\": \"...\", \"branch\": \"...\"})"
    )

    schema = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "description": "Whether the task succeeded"},
            "result": {
                "type": "object",
                "description": "The final result object (role-specific schema)",
                "additionalProperties": True,
            },
        },
        "required": ["ok"],
    }

    def __call__(self, **kwargs):
        # This should never be called directly — the harness intercepts
        # finish() calls and returns the arguments as the result.
        return kwargs

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "rfc_doc": {"type": "string"},
                        "branch": {"type": "string"},
                        "commits": {"type": "array", "items": {"type": "string"}},
                        "status": {"type": "string"},
                        "comments": {"type": "array"},
                        "results": {"type": "array"},
                        "run_id": {"type": "string"},
                        "error": {"type": "string"},
                    },
                    "required": ["ok"],
                },
            },
        }
