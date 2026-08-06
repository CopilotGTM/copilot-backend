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
from typing import Any, ClassVar
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

from .base_mixins import utcnow
from .tenant_constraints import tenant_foreign_key

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


class AutomationTriggerFamily(str, Enum):
    """Registry family that determines how an invocation is routed."""

    EVENT = "event"
    MANUAL = "manual"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"


class AutomationPrincipalType(str, Enum):
    """Authority used by a run after activation-time revalidation."""

    USER = "user"
    ORGANISATION_SERVICE = "organisation_service"


class AutomationScopeType(str, Enum):
    """Data ownership boundary applied while matching a trigger."""

    OWNED_OR_CREATED_BY_PRINCIPAL = "owned_or_created_by_principal"
    ORGANISATION = "organisation"


class AutomationAudienceMode(str, Enum):
    """Users allowed to discover a generated run or delivery."""

    PRINCIPAL = "principal"
    NONE = "none"
    EXPLICIT_USERS = "explicit_users"
    SUBJECT_PARTICIPANTS = "subject_participants"


class AutomationRouteMode(str, Enum):
    """Whether an event is matched broadly or sent to one trigger."""

    MATCH = "match"
    DIRECT = "direct"


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

    __tablename__: ClassVar[str] = "user_agents"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "agent_id",
            name="uq_user_agents_org_agent_id",
        ),
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
    """Compiled registry trigger with pinned authority and executable target."""

    __tablename__: ClassVar[str] = "automation_triggers"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "trigger_id",
            name="uq_automation_triggers_org_trigger_id",
        ),
        Index(
            "ix_automation_triggers_target",
            "organisation_id",
            "created_by_user_id",
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
                "status = 'active' AND family = 'schedule' AND next_trigger_at IS NOT NULL"
            ),
        ),
        Index(
            "ix_automation_triggers_active_event_keys",
            "match_event_keys",
            postgresql_using="gin",
            postgresql_where=text("status = 'active' AND family = 'event'"),
        ),
        Index(
            "uq_automation_triggers_active_config",
            "organisation_id",
            "created_by_user_id",
            "target_type",
            "target_id",
            "config_hash",
            unique=True,
            postgresql_where=text("status <> 'archived'"),
        ),
        Index(
            "uq_automation_triggers_active_org_service_config",
            "organisation_id",
            "target_type",
            "target_id",
            "config_hash",
            unique=True,
            postgresql_where=text(
                "status <> 'archived' AND execution_principal_type = 'organisation_service'"
            ),
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
            "family IN ('event', 'manual', 'schedule', 'webhook')",
            name="ck_automation_triggers_family",
        ),
        CheckConstraint(
            "cardinality(match_event_keys) >= 1",
            name="ck_automation_triggers_match_event_keys",
        ),
        CheckConstraint(
            "config_schema_version >= 1",
            name="ck_automation_triggers_config_schema_version",
        ),
        CheckConstraint(
            "("
            "execution_principal_type = 'user' "
            "AND scope_type = 'owned_or_created_by_principal' "
            "AND audience_mode = 'principal'"
            ") OR ("
            "execution_principal_type = 'organisation_service' "
            "AND execution_principal_id = organisation_id "
            "AND scope_type = 'organisation' "
            "AND audience_mode IN ('none', 'explicit_users', 'subject_participants')"
            ")",
            name="ck_automation_triggers_principal_scope_audience",
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

    trigger_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(description="Tenant boundary for the trigger.")
    created_by_user_id: UUID = Field(
        description="User who created and manages this configuration.",
    )
    name: str = Field(sa_column=Column(Text, nullable=False))
    target_type: AutomationTargetType = Field(
        default=AutomationTargetType.AGENT,
        sa_column=Column(Text, nullable=False, server_default=text("'agent'")),
    )
    # The target is polymorphic so a workflow runtime can reuse this table later.
    # Activation resolves it and stores the immutable target snapshot below.
    target_id: UUID = Field(description="Identifier resolved according to target_type.")
    target_revision: int = Field(sa_column=Column(Integer, nullable=False))
    target_snapshot: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    trigger_key: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Stable key from the code-owned trigger registry.",
    )
    family: AutomationTriggerFamily = Field(sa_column=Column(Text, nullable=False))
    match_event_keys: list[str] = Field(
        sa_column=Column(ARRAY(Text), nullable=False),
        description="Compiled event keys used by the candidate GIN lookup.",
    )
    config_schema_version: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=_ONE_DEFAULT),
    )
    trigger_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
    )
    watch_config: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    filter_config: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    execution_principal_type: AutomationPrincipalType = Field(
        sa_column=Column(Text, nullable=False),
    )
    execution_principal_id: UUID = Field(
        description="User or organisation service identity used at execution.",
    )
    scope_type: AutomationScopeType = Field(sa_column=Column(Text, nullable=False))
    scope_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
    )
    audience_mode: AutomationAudienceMode = Field(sa_column=Column(Text, nullable=False))
    audience_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
    )
    task_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
    )
    delivery_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
    )
    config_hash: str = Field(
        sa_column=Column(Text, nullable=False),
        description="SHA-256 of the server-canonicalized effective configuration.",
    )
    status: AutomationTriggerStatus = Field(
        default=AutomationTriggerStatus.ACTIVE,
        sa_column=Column(Text, nullable=False, server_default=_ACTIVE_DEFAULT),
    )
    next_trigger_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_triggered_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    trigger_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=_ZERO_DEFAULT),
    )
    consecutive_failures: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=_ZERO_DEFAULT),
    )
    last_error_code: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
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
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        ),
    )


class AutomationEventDispatch(SQLModel, table=True):
    """Immutable canonical event plus durable activation bookkeeping."""

    __tablename__: ClassVar[str] = "automation_event_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "event_id",
            name="uq_automation_event_dispatches_org_event_id",
        ),
        tenant_foreign_key(
            "automation_event_dispatches",
            "direct_trigger_id",
            "automation_triggers",
            "trigger_id",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_automation_event_dispatch_idempotency",
            "organisation_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_automation_event_dispatch_direct_trigger",
            "organisation_id",
            "direct_trigger_id",
        ),
        Index(
            "ix_automation_event_dispatch_pending",
            "created_at",
            "event_id",
            postgresql_where=text("processed_at IS NULL AND failed_at IS NULL"),
        ),
        Index(
            "ix_automation_event_dispatch_event_created",
            "organisation_id",
            "event_key",
            text("created_at DESC"),
        ),
        CheckConstraint(
            "(route_mode = 'match' AND direct_trigger_id IS NULL) "
            "OR (route_mode = 'direct' AND direct_trigger_id IS NOT NULL)",
            name="ck_automation_event_dispatch_route",
        ),
        CheckConstraint(
            "candidate_count >= 0 AND matched_count >= 0 AND matched_count <= candidate_count",
            name="ck_automation_event_dispatch_counts",
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

    event_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(description="Tenant boundary from the envelope.")
    event_key: str = Field(sa_column=Column(Text, nullable=False))
    route_mode: AutomationRouteMode = Field(sa_column=Column(Text, nullable=False))
    direct_trigger_id: UUID | None = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True),
    )
    idempotency_key: str = Field(sa_column=Column(Text, nullable=False))
    envelope: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    activation_outcomes: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_ARRAY_DEFAULT),
    )
    candidate_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=_ZERO_DEFAULT),
    )
    matched_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=_ZERO_DEFAULT),
    )
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
    last_error_code: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
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
    """One immutable trigger/event activation decision and its execution state."""

    __tablename__: ClassVar[str] = "agent_runs"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "run_id",
            name="uq_agent_runs_org_run_id",
        ),
        tenant_foreign_key(
            "agent_runs",
            "agent_id",
            "user_agents",
            "agent_id",
            ondelete="RESTRICT",
        ),
        tenant_foreign_key(
            "agent_runs",
            "trigger_id",
            "automation_triggers",
            "trigger_id",
            ondelete="RESTRICT",
        ),
        tenant_foreign_key(
            "agent_runs",
            "event_id",
            "automation_event_dispatches",
            "event_id",
            ondelete="RESTRICT",
        ),
        tenant_foreign_key(
            "agent_runs",
            "caused_by_run_id",
            "agent_runs",
            "run_id",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_agent_runs_creator_created",
            "organisation_id",
            "created_by_user_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_agent_runs_principal_created",
            "organisation_id",
            "execution_principal_type",
            "execution_principal_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_agent_runs_agent_created",
            "organisation_id",
            "agent_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_agent_runs_trigger_created",
            "organisation_id",
            "trigger_id",
            text("created_at DESC"),
        ),
        Index("ix_agent_runs_event", "organisation_id", "event_id"),
        Index("ix_agent_runs_causation", "organisation_id", "caused_by_run_id"),
        Index(
            "ix_agent_runs_pending_claim",
            "created_at",
            "run_id",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_agent_runs_trigger_event",
            "organisation_id",
            "trigger_id",
            "event_id",
            unique=True,
        ),
        CheckConstraint(
            "("
            "execution_principal_type = 'user' "
            "AND audience_mode = 'principal'"
            ") OR ("
            "execution_principal_type = 'organisation_service' "
            "AND execution_principal_id = organisation_id "
            "AND audience_mode IN ('none', 'explicit_users', 'subject_participants')"
            ")",
            name="ck_agent_runs_principal_audience",
        ),
        CheckConstraint("agent_revision >= 1", name="ck_agent_runs_agent_revision"),
        CheckConstraint(
            "status IN ("
            "'pending', 'running', 'succeeded', 'failed', 'delivery_failed', "
            "'validation_failed', 'cancelled'"
            ")",
            name="ck_agent_runs_status",
        ),
        CheckConstraint(
            "causation_depth BETWEEN 0 AND 3",
            name="ck_agent_runs_causation_depth",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_agent_runs_attempt_count"),
        CheckConstraint("cost IS NULL OR cost >= 0", name="ck_agent_runs_cost"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_agent_runs_latency_ms",
        ),
    )

    run_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(description="Tenant boundary copied at activation.")
    created_by_user_id: UUID = Field(description="User who created the matched trigger.")
    execution_principal_type: AutomationPrincipalType = Field(
        sa_column=Column(Text, nullable=False),
    )
    execution_principal_id: UUID = Field(
        description="Revalidated user or organisation service identity.",
    )
    audience_mode: AutomationAudienceMode = Field(sa_column=Column(Text, nullable=False))
    audience_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
    )
    agent_id: UUID = Field(sa_column=Column(sa.Uuid, nullable=False))
    trigger_id: UUID = Field(sa_column=Column(sa.Uuid, nullable=False))
    agent_revision: int = Field(sa_column=Column(Integer, nullable=False))
    status: AgentRunStatus = Field(
        default=AgentRunStatus.PENDING,
        sa_column=Column(Text, nullable=False, server_default=text("'pending'")),
    )
    idempotency_key: str = Field(sa_column=Column(Text, nullable=False))
    event_id: UUID = Field(sa_column=Column(sa.Uuid, nullable=False))
    event_key: str = Field(sa_column=Column(Text, nullable=False))
    scheduled_for: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    correlation_id: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Trace identity shared by the event chain.",
    )
    caused_by_run_id: UUID | None = Field(
        default=None,
        sa_column=Column(sa.Uuid, nullable=True),
    )
    causation_depth: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=_ZERO_DEFAULT),
    )
    trigger_snapshot: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    agent_snapshot: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    input_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=_EMPTY_OBJECT_DEFAULT),
    )
    output_text: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    output_json: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    error_code: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    error_message: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=_ZERO_DEFAULT),
    )
    claimed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    claimed_by: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    model_name: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    token_usage: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    cost: Decimal | None = Field(
        default=None,
        sa_column=Column(Numeric, nullable=True),
    )
    latency_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
