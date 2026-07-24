"""Persistence models for user-owned agents, reusable triggers, and execution history.

Agent definitions and trigger definitions have independent lifecycles: editing one never
silently rewrites the other. A trigger keeps a polymorphic target identity so workflows can
reuse the same table later, while each run stores pinned snapshots and worker claim state for
durable replay, idempotency, and auditing.

The database keeps stable relational invariants and proven access paths only. Services own
evolving JSON shapes, catalog membership, and lifecycle policy.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from .base_mixins import utcnow

_EMPTY_OBJECT_DEFAULT = text("'{}'::jsonb")
_EMPTY_ARRAY_DEFAULT = text("'[]'::jsonb")
_ZERO_DEFAULT = text("0")
_ONE_DEFAULT = text("1")
_ACTIVE_DEFAULT = text("'active'")


class AgentSourceType(str, Enum):
    """Provenance of a materialised user agent."""

    USER = "user"
    SYSTEM = "system"


class AgentVisibility(str, Enum):
    """Audience allowed to discover an agent definition."""

    PRIVATE = "private"
    ORGANISATION = "organisation"


class AgentStatus(str, Enum):
    """User-agent definition lifecycle."""

    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class AgentOutputMode(str, Enum):
    """Shape expected from a completed agent execution."""

    TEXT = "text"
    JSON = "json"


class AutomationTargetType(str, Enum):
    """Kind of executable definition selected by a trigger."""

    AGENT = "agent"
    WORKFLOW = "workflow"


class AutomationTriggerType(str, Enum):
    """Invocation source supported by the generic trigger contract."""

    SCHEDULE = "schedule"
    EVENT = "event"


class AutomationScopeType(str, Enum):
    """Data ownership boundary applied while matching a trigger."""

    OWNER = "owner"


class AutomationTriggerStatus(str, Enum):
    """Persistent trigger lifecycle, separate from transient run state."""

    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class AgentRunStatus(str, Enum):
    """Durable dispatch and terminal states for one agent execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DELIVERY_FAILED = "delivery_failed"
    VALIDATION_FAILED = "validation_failed"
    CANCELLED = "cancelled"


class UserAgent(SQLModel, table=True):
    """Current executable definition owned by one user inside one organisation."""

    __tablename__ = "user_agents"
    __table_args__ = (
        Index(
            "ix_user_agents_owner_status_updated",
            "organisation_id",
            "owner_user_id",
            "status",
            text("updated_at DESC"),
        ),
        CheckConstraint("revision >= 1", name="ck_user_agents_revision"),
        CheckConstraint(
            "source_type IN ('user', 'system')",
            name="ck_user_agents_source_type",
        ),
        CheckConstraint(
            "visibility IN ('private', 'organisation')",
            name="ck_user_agents_visibility",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'archived')",
            name="ck_user_agents_status",
        ),
        CheckConstraint(
            "output_mode IN ('text', 'json')",
            name="ck_user_agents_output_mode",
        ),
    )

    agent_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Opaque identifier for this user-owned agent.",
    )
    organisation_id: UUID = Field(
        description="Tenant boundary copied from the authenticated organisation.",
    )
    owner_user_id: UUID = Field(
        description="User who can manage and execute this private agent.",
    )
    source_type: AgentSourceType = Field(
        default=AgentSourceType.USER,
        sa_column=Column(Text, nullable=False, server_default=text("'user'")),
        description="Whether this definition was user-created or copied from a system template.",
    )
    source_agent_id: UUID | None = Field(
        default=None,
        description="Stable system-template identifier when source_type is system.",
    )
    source_version: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description="Version of the system template materialised into this row.",
    )
    revision: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=_ONE_DEFAULT),
        description="Monotonic version incremented after every execution-affecting edit.",
    )
    visibility: AgentVisibility = Field(
        default=AgentVisibility.PRIVATE,
        sa_column=Column(Text, nullable=False, server_default=text("'private'")),
        description="Discovery boundary; V1 writes only private agents.",
    )
    status: AgentStatus = Field(
        default=AgentStatus.ACTIVE,
        sa_column=Column(Text, nullable=False, server_default=_ACTIVE_DEFAULT),
        description="Definition lifecycle; archive replaces public hard delete.",
    )
    agent_type: str = Field(
        default="general",
        sa_column=Column(Text, nullable=False, server_default=text("'general'")),
        description="Runtime implementation family used to execute this definition.",
    )
    name: str = Field(
        sa_column=Column(Text, nullable=False),
        description="User-facing agent name.",
    )
    description: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Optional user-facing explanation of the agent purpose.",
    )
    prompt: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Base instructions supplied to the agent runtime.",
    )
    model_name: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Optional model override; null uses the runtime default.",
    )
    # Pydantic reserves model_config for class configuration, so this Python name maps
    # to the product's required model_config database column.
    model_settings: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            "model_config",
            JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT_DEFAULT,
        ),
        description="Model parameters stored without credentials.",
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_ARRAY_DEFAULT),
        description="Stable code-owned tool keys explicitly granted to this agent.",
    )
    allowed_skills: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_ARRAY_DEFAULT),
        description="Stable code-owned skill keys explicitly granted to this agent.",
    )
    max_turns: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description="Optional positive runtime turn limit.",
    )
    input_schema: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Optional JSON Schema used to validate resolved runtime input.",
    )
    output_mode: AgentOutputMode = Field(
        default=AgentOutputMode.TEXT,
        sa_column=Column(Text, nullable=False, server_default=text("'text'")),
        description="Whether execution returns plain text or validated JSON.",
    )
    output_schema: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="JSON Schema required when output_mode is json.",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        description="Agent creation time.",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        ),
        description="Last definition edit time.",
    )


class AutomationTrigger(SQLModel, table=True):
    """User-owned invocation rule pinned to an exact executable target revision.

    target_id intentionally has no database foreign key because target_type can identify an
    agent now or a workflow later. The service layer must resolve target existence, tenant,
    ownership, lifecycle, and revision before activation.
    """

    __tablename__ = "automation_triggers"
    __table_args__ = (
        Index(
            "ix_automation_triggers_target",
            "organisation_id",
            "owner_user_id",
            "target_type",
            "target_id",
            "status",
            text("updated_at DESC"),
        ),
        Index(
            "ix_automation_triggers_due_schedule",
            "next_trigger_at",
            "trigger_id",
            postgresql_where=text(
                "status = 'active' AND trigger_type = 'schedule' AND next_trigger_at IS NOT NULL"
            ),
        ),
        Index(
            "ix_automation_triggers_active_event",
            "organisation_id",
            "source_domain",
            "event_type",
            "trigger_id",
            postgresql_where=text("status = 'active' AND trigger_type = 'event'"),
        ),
        Index(
            "uq_automation_triggers_active_config",
            "organisation_id",
            "owner_user_id",
            "target_type",
            "target_id",
            "config_hash",
            unique=True,
            postgresql_where=text("status <> 'archived'"),
        ),
        CheckConstraint(
            "target_type IN ('agent', 'workflow')",
            name="ck_automation_triggers_target_type",
        ),
        CheckConstraint(
            "target_revision >= 1",
            name="ck_automation_triggers_target_revision",
        ),
        CheckConstraint(
            "trigger_type IN ('schedule', 'event')",
            name="ck_automation_triggers_trigger_type",
        ),
        CheckConstraint(
            "config_schema_version >= 1",
            name="ck_automation_triggers_config_schema_version",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'archived')",
            name="ck_automation_triggers_status",
        ),
        CheckConstraint(
            "trigger_count >= 0 AND consecutive_failures >= 0",
            name="ck_automation_triggers_counts",
        ),
    )

    trigger_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Opaque identifier for this automation trigger.",
    )
    organisation_id: UUID = Field(
        description="Tenant boundary copied from authenticated context.",
    )
    owner_user_id: UUID = Field(
        description="User whose data scope, credentials, and lifecycle apply.",
    )
    name: str = Field(
        sa_column=Column(Text, nullable=False),
        description="User-facing label for the automation job.",
    )
    target_type: AutomationTargetType = Field(
        default=AutomationTargetType.AGENT,
        sa_column=Column(Text, nullable=False, server_default=text("'agent'")),
        description="Executable target family; V1 services accept only agent.",
    )
    target_id: UUID = Field(
        description="Polymorphic identifier resolved according to target_type.",
    )
    target_revision: int = Field(
        sa_column=Column(Integer, nullable=False),
        description="Exact target revision pinned when the trigger is activated.",
    )
    target_snapshot: dict[str, Any] = Field(
        sa_column=Column(JSONB, nullable=False),
        description="Server-owned immutable executable snapshot for target_revision.",
    )
    trigger_type: AutomationTriggerType = Field(
        sa_column=Column(Text, nullable=False),
        description="Whether this trigger matches a due schedule or a source event.",
    )
    source_domain: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Event family such as email, calendar, CRM, or a future provider.",
    )
    event_type: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Stable semantic event key within source_domain.",
    )
    config_schema_version: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=_ONE_DEFAULT),
        description="Version used to parse and validate trigger JSON configurations.",
    )
    trigger_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
        description="Schedule or source-specific invocation configuration.",
    )
    filter_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
        description="Event predicates evaluated after relational candidate lookup.",
    )
    scope_type: AutomationScopeType = Field(
        default=AutomationScopeType.OWNER,
        sa_column=Column(Text, nullable=False, server_default=text("'owner'")),
        description="Data ownership boundary; V1 permits owner scope only.",
    )
    scope_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
        description="Optional entity, deal, or integration restrictions within owner scope.",
    )
    task_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
        description="Per-trigger goal, input mapping, and safe runtime overrides.",
    )
    delivery_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
        description="Result destination identifiers without provider credentials.",
    )
    config_hash: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Canonical SHA-256 identity used to reject exact active duplicates.",
    )
    status: AutomationTriggerStatus = Field(
        default=AutomationTriggerStatus.ACTIVE,
        sa_column=Column(Text, nullable=False, server_default=_ACTIVE_DEFAULT),
        description="Configuration lifecycle; run execution never toggles this transiently.",
    )
    next_trigger_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Next UTC occurrence projected for schedule dispatch.",
    )
    last_triggered_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent successful execution time.",
    )
    trigger_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=_ZERO_DEFAULT),
        description="Count of successful executions.",
    )
    consecutive_failures: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=_ZERO_DEFAULT),
        description="Back-to-back terminal failures since the last success.",
    )
    last_error: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Latest sanitised operational error summary.",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        description="Trigger creation time.",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        ),
        description="Last trigger configuration edit time.",
    )


class AutomationEventDispatch(SQLModel, table=True):
    """Durable activation state for one semantic source-event occurrence.

    Producers write this row in the source transaction. Chat-worker recovery
    may publish it repeatedly; the unique source identity and idempotent agent
    runs make duplicate delivery safe.
    """

    __tablename__ = "automation_event_dispatches"
    __table_args__ = (
        Index(
            "uq_automation_event_dispatch_source",
            "organisation_id",
            "source_domain",
            "source_event_id",
            "event_type",
            unique=True,
        ),
        Index(
            "ix_automation_event_dispatch_pending",
            "created_at",
            "dispatch_id",
            postgresql_where=text("processed_at IS NULL AND failed_at IS NULL"),
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_automation_event_dispatch_attempts",
        ),
        CheckConstraint(
            "NOT (processed_at IS NOT NULL AND failed_at IS NOT NULL)",
            name="ck_automation_event_dispatch_terminal_state",
        ),
    )

    dispatch_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(description="Tenant boundary of the source event.")
    source_domain: str = Field(sa_column=Column(Text, nullable=False))
    source_event_id: str = Field(sa_column=Column(Text, nullable=False))
    event_type: str = Field(sa_column=Column(Text, nullable=False))
    attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=_ZERO_DEFAULT),
    )
    last_attempted_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    processed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    failed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_error: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )


class AgentRun(SQLModel, table=True):
    """Durable execution attempt and immutable audit record for one agent run."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index(
            "ix_agent_runs_owner_created",
            "organisation_id",
            "owner_user_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_agent_runs_agent_created",
            "agent_id",
            "organisation_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_agent_runs_trigger_created",
            "trigger_id",
            "organisation_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_agent_runs_pending_claim",
            "created_at",
            "run_id",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_agent_runs_idempotency",
            "organisation_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        CheckConstraint(
            "agent_revision >= 1",
            name="ck_agent_runs_agent_revision",
        ),
        CheckConstraint(
            "status IN ("
            "'pending', 'running', 'succeeded', 'failed', 'delivery_failed', "
            "'validation_failed', 'cancelled'"
            ")",
            name="ck_agent_runs_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_agent_runs_attempt_count",
        ),
        CheckConstraint(
            "cost IS NULL OR cost >= 0",
            name="ck_agent_runs_cost",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_agent_runs_latency_ms",
        ),
    )

    run_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Opaque identifier for this durable execution attempt.",
    )
    organisation_id: UUID = Field(
        description="Tenant boundary copied at dispatch time.",
    )
    owner_user_id: UUID = Field(
        description="Execution principal copied at dispatch time.",
    )
    agent_id: UUID = Field(
        sa_column=Column(
            sa.Uuid,
            ForeignKey("user_agents.agent_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        description="Agent definition whose pinned revision was executed.",
    )
    trigger_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid,
            ForeignKey("automation_triggers.trigger_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        description="Causing trigger, or null for a manual run.",
    )
    agent_revision: int = Field(
        sa_column=Column(Integer, nullable=False),
        description="Exact agent revision represented by agent_snapshot.",
    )
    status: AgentRunStatus = Field(
        default=AgentRunStatus.PENDING,
        sa_column=Column(Text, nullable=False, server_default=text("'pending'")),
        description="Durable dispatch, claim, and terminal execution state.",
    )
    idempotency_key: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Stable source-occurrence key used to make dispatch replay-safe.",
    )
    source_domain: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Event source family copied from the matching trigger.",
    )
    event_type: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Semantic event key copied from the matching trigger.",
    )
    source_event_id: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Provider or internal event identifier; text permits non-UUID values.",
    )
    scheduled_for: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Due occurrence represented by a scheduled run.",
    )
    trigger_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
        description="Exact trigger, filter, scope, task, and delivery configuration matched.",
    )
    agent_snapshot: dict[str, Any] = Field(
        sa_column=Column(JSONB, nullable=False),
        description="Exact server-owned agent configuration executed by the worker.",
    )
    input_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
        description="Resolved event data and mapped runtime input.",
    )
    output_text: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Human-readable execution output, when produced.",
    )
    output_json: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Structured output validated against the pinned output schema.",
    )
    error_code: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Stable machine-readable terminal failure code.",
    )
    error_message: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Sanitised user-facing diagnostic summary.",
    )
    attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=_ZERO_DEFAULT),
        description="Number of worker attempts made for this durable run.",
    )
    claimed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Current or last worker claim time used for lease recovery.",
    )
    claimed_by: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Worker identity holding or last holding the execution claim.",
    )
    model_name: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Effective model used for this execution.",
    )
    token_usage: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Input, output, reasoning, and cache token counts.",
    )
    cost: Decimal | None = Field(
        default=None,
        sa_column=Column(Numeric, nullable=True),
        description="Estimated model cost for this execution.",
    )
    latency_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description="End-to-end execution latency in milliseconds.",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        description="Durable dispatch creation time.",
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Time external agent execution began.",
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Time the run reached a terminal state.",
    )
