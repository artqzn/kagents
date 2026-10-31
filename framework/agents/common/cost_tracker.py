"""LiteLLM CustomLogger for cost tracking."""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
import os

try:
    from litellm.integrations.custom_logger import CustomLogger
    import litellm
    HAS_LITELLM = True
except ImportError:
    HAS_LITELLM = False
    CustomLogger = object  # type: ignore

COST_LOG = Path(os.environ.get(
    "KAG_WORKSPACE", "/workspace"
)) / "state" / "llm_costs.jsonl"


class CostTracker(CustomLogger):
    """Logs per-call cost and latency to a JSONL file."""

    def log_success_event(self, kwargs, completion_response,
                          start_time, end_time):
        try:
            cost = litellm.completion_cost(
                completion_response=completion_response
            )
        except Exception:
            cost = 0.0

        try:
            COST_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "model": getattr(completion_response, "model", "unknown"),
                "agent": kwargs.get("metadata", {}).get(
                    "agent", "unknown"
                ),
                "task_id": kwargs.get("metadata", {}).get("task_id"),
                "cost_usd": cost,
                "duration_sec": (end_time - start_time).total_seconds(),
                "prompt_tokens": completion_response.usage.prompt_tokens,
                "completion_tokens": completion_response.usage.completion_tokens,
                "timestamp": end_time.isoformat(),
            }
            with COST_LOG.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            # Don't let logging failures crash the agent
            print(f"Warning: cost tracking failed: {e}", flush=True)


_tracker = None


def register_cost_tracker():
    """Register the cost tracker with LiteLLM (idempotent)."""
    global _tracker
    if not HAS_LITELLM:
        return
    if _tracker is None:
        _tracker = CostTracker()
        existing = litellm.callbacks or []
        if _tracker not in existing:
            litellm.callbacks = existing + [_tracker]
