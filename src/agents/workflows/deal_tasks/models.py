"""Structured output contracts for the deal task automation agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DealTaskItemResult(BaseModel):
    """One attempted task action from the deal task agent."""

    task_id: str | None = None
    title: str
    action: str
    status: str
    reason: str | None = None


class DealTaskSyncResult(BaseModel):
    """Summary of task create/complete actions performed for one deal."""

    created_tasks: list[DealTaskItemResult] = Field(default_factory=list)
    completed_tasks: list[DealTaskItemResult] = Field(default_factory=list)
    skipped_items: list[DealTaskItemResult] = Field(default_factory=list)
    failed_items: list[DealTaskItemResult] = Field(default_factory=list)
    created_count: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0


__all__ = ["DealTaskItemResult", "DealTaskSyncResult"]
