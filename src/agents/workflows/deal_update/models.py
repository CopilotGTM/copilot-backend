"""Pydantic models for outputs produced by the deal-update workflow."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DealUpdateStatus = Literal["applied", "skipped", "approval_required", "failed"]


class DealUpdateItem(BaseModel):
    """Single normalized update action emitted by the workflow.

    This unit captures each attempted write/skip/approval decision and the
    caller-visible reason used by clients.
    """

    action: str
    target: str
    status: DealUpdateStatus
    field: str | None = None
    old_value: Any | None = None
    new_value: Any | None = None
    reason: str
    tool_result: dict[str, Any] | None = None


class DealUpdateResult(BaseModel):
    """Container for all attempts in one execution of DealUpdateAgent."""

    applied: list[DealUpdateItem] = Field(default_factory=list)
    skipped: list[DealUpdateItem] = Field(default_factory=list)
    approval_required: list[DealUpdateItem] = Field(default_factory=list)
    failed: list[DealUpdateItem] = Field(default_factory=list)
    applied_count: int = 0
    skipped_count: int = 0
    approval_required_count: int = 0
    failed_count: int = 0
