"""HTTP response contracts for audience-scoped agent execution history."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from src.domain.models.organisation_tables import (
    AgentRunStatus,
    AutomationAudienceMode,
    AutomationPrincipalType,
)

# Delivery presentation is deliberately separate from AgentRunStatus: a model
# run can be complete while one or more post-run notification targets need repair.
AgentRunDeliveryPresentationStatus = Literal[
    "pending",
    "sending",
    "sent",
    "partially_sent",
    "failed",
    "skipped",
]


class AgentRunOutputDeliverySnapshot(BaseModel):
    """Immutable external-channel decision captured when the run was created."""

    model_config = ConfigDict(extra="forbid")

    channels: list[Literal["email_notification"]] = Field(default_factory=list)


class AgentRunDeliveryTargetSummary(BaseModel):
    """One internal delivery target; IDs are for API actions, not UI labels."""

    delivery_id: UUID
    status: AgentRunDeliveryPresentationStatus
    error_code: str | None = None
    error_message: str | None = None
    can_retry: bool = False
    sent_at: datetime | None = None


class AgentRunDeliverySummary(BaseModel):
    """Independent post-run delivery state safe for run-history presentation."""

    channel: Literal["email"]
    status: AgentRunDeliveryPresentationStatus
    recipient_count: int
    sent_count: int
    failed_count: int
    skipped_count: int
    can_retry: bool = False
    sent_at: datetime | None = None
    targets: list[AgentRunDeliveryTargetSummary] = Field(default_factory=list)


class AgentRunDeliveryRetryRequest(BaseModel):
    """Select one visible notification target without rerunning its agent."""

    delivery_id: UUID


class AgentRunDeliveryRetryResponse(BaseModel):
    """Accepted repair retry for one owner-visible delivery."""

    run_id: UUID
    status: Literal["pending"]
    delivery_count: int


class AgentRunSummary(BaseModel):
    """Compact lifecycle, event identity, authority, and result preview."""

    run_id: UUID
    agent_id: UUID
    trigger_id: UUID
    agent_revision: int
    status: AgentRunStatus
    event_id: UUID
    event_key: str
    correlation_id: str
    execution_principal_type: AutomationPrincipalType
    audience_mode: AutomationAudienceMode
    subject_type: str | None
    subject_id: str | None
    attempt_count: int
    model_name: str | None
    latency_ms: int | None
    error_code: str | None
    error_message: str | None
    output_preview: str | None
    delivery: AgentRunDeliverySummary | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AgentRunListResponse(BaseModel):
    """Cursor page of runs visible to the authenticated audience member."""

    items: list[AgentRunSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class AgentRunDetail(AgentRunSummary):
    """Full immutable execution input, result, usage, and causation metadata."""

    created_by_user_id: UUID
    execution_principal_id: UUID
    audience_config: dict[str, Any]
    scheduled_for: datetime | None
    caused_by_run_id: UUID | None
    causation_depth: int
    source_domain: str
    source_system: str
    source_operation: str
    source_event_id: str
    actor_type: str
    actor_id: str | None
    claimed_at: datetime | None
    claimed_by: str | None
    token_usage: dict[str, Any] | None
    cost: Decimal | None
    output_text: str | None
    output_json: Any | None
    output_delivery: AgentRunOutputDeliverySnapshot
    trigger_snapshot: dict[str, Any]
    agent_snapshot: dict[str, Any]
    input_snapshot: dict[str, Any]
