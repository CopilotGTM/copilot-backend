"""HTTP response contracts for owner-scoped agent execution history."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from src.domain.models.organisation_tables import AgentRunStatus


class AgentRunSummary(BaseModel):
    """Compact lifecycle and result preview for one agent run."""

    run_id: UUID
    agent_id: UUID
    trigger_id: UUID | None
    agent_revision: int
    status: AgentRunStatus
    event_type: str | None
    attempt_count: int
    model_name: str | None
    latency_ms: int | None
    error_code: str | None
    error_message: str | None
    output_preview: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AgentRunListResponse(BaseModel):
    """Cursor page of owned agent runs."""

    items: list[AgentRunSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class AgentRunDetail(BaseModel):
    """Full persisted execution contract, result, usage, and timing."""

    run_id: UUID
    agent_id: UUID
    trigger_id: UUID | None
    agent_revision: int
    status: AgentRunStatus
    source_domain: str | None
    event_type: str | None
    source_event_id: str | None
    scheduled_for: datetime | None
    attempt_count: int
    claimed_at: datetime | None
    claimed_by: str | None
    model_name: str | None
    token_usage: dict[str, Any] | None
    cost: Decimal | None
    latency_ms: int | None
    error_code: str | None
    error_message: str | None
    output_text: str | None
    output_json: Any | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    trigger_snapshot: dict[str, Any]
    agent_snapshot: dict[str, Any]
    input_snapshot: dict[str, Any]
