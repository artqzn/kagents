"""Typed schemas for tickets and agent results."""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Literal, Optional


class Ticket(BaseModel):
    """A task ticket passed through the agent pipeline."""

    task_id: str
    title: str
    description: str
    subsystem: str = "mm"
    rfc_doc: Optional[str] = None
    branch_impl: Optional[str] = None
    worktree_path: Optional[str] = None  # Path to git worktree for implementation
    review_comments: list[dict] = Field(default_factory=list)
    test_results: list[dict] = Field(default_factory=list)
    iteration_feedback: Optional[str] = None  # passed to Implementer on retry
    status: Literal[
        "planning", "impl", "review", "test", "merged", "failed"
    ] = "planning"
    iteration: int = 0
    max_iterations: int = 3


class PlannerResult(BaseModel):
    ok: bool
    rfc_doc: Optional[str] = None
    branch: Optional[str] = None
    error: Optional[str] = None


class ImplementerResult(BaseModel):
    ok: bool
    branch: Optional[str] = None
    commits: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class ReviewerResult(BaseModel):
    """Three-state: ok (no blockers), blocked (has blockers), failed (crashed)."""
    status: Literal["ok", "blocked", "failed"]
    comments: list[dict] = Field(default_factory=list)
    error: Optional[str] = None


class TesterResult(BaseModel):
    status: Literal["ok", "failed", "skipped"]
    results: list[dict] = Field(default_factory=list)
    run_id: Optional[str] = None
    error: Optional[str] = None
