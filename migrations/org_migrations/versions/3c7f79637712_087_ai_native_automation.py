"""Replace the prototype automation runtime with the AI-native contract.

The existing trigger, dispatch, and run tables are empty, unreleased prototype
schemas. Recreating them keeps the clean contract legible and avoids carrying
transitional states into activation code.

Revision ID: 3c7f79637712
Revises: 3c7f79637713
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from migrations.org_migrations.tenant_identity import validate_migration_schema_name
from migrations.org_migrations.tenant_policy import protect_tenant_table
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637712"
down_revision: str | None = "3c7f79637713"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_OBJECT = sa.text("'{}'::jsonb")
_EMPTY_ARRAY = sa.text("'[]'::jsonb")
_ZERO = sa.text("0")


def _migration_schema() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    schema_name = x_args.get("schema")
    if schema_name is None:
        raise ValueError("schema is required for organisation migrations")
    return validate_migration_schema_name(schema_name)


def _assert_runtime_tables_empty() -> None:
    schema_name = _migration_schema()
    bind = op.get_bind()
    for table_name in (
        "automation_triggers",
        "automation_event_dispatches",
        "agent_runs",
    ):
        # copilotgtm: The schema passes the tenant-name validator and table names
        # come only from this fixed tuple; SQL parameters cannot bind identifiers.
        has_rows = bind.execute(
            sa.text(
                f'SELECT EXISTS (SELECT 1 FROM "{schema_name}"."{table_name}" LIMIT 1)'  # noqa: S608
            )
        ).scalar()
        if has_rows:
            raise RuntimeError(
                f"Refusing to replace automation runtime: {table_name} is not empty"
            )


def upgrade() -> None:
    """Recreate automation runtime tables and restore forced tenant RLS."""

    # Replacing tables is safe only while the prototype has no rows; refusing
    # non-empty tables prevents this migration from silently discarding runtime work.
    _assert_runtime_tables_empty()

    # Add canonical event snapshots and causation lineage to existing CRM facts
    # before the new runtime tables begin referencing those facts.
    op.add_column(
        "crm_mutation_events",
        sa.Column(
            "record_snapshot",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
    )
    op.add_column(
        "crm_mutation_events",
        sa.Column(
            "automation_source_mode",
            sa.String(length=32),
            nullable=False,
            server_default="system",
        ),
    )
    op.add_column(
        "crm_mutation_events",
        sa.Column("caused_by_run_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "crm_mutation_events",
        sa.Column(
            "causation_depth",
            sa.Integer(),
            nullable=False,
            server_default=_ZERO,
        ),
    )
    op.create_check_constraint(
        "ck_crm_mutation_events_causation_depth",
        "crm_mutation_events",
        "causation_depth BETWEEN 0 AND 3",
    )

    # The old prototype tables are empty and lack the composite tenant keys the
    # AI-native runtime uses for every cross-table relationship.
    op.drop_table("agent_runs")
    op.drop_table("automation_event_dispatches")
    op.drop_table("automation_triggers")

    # Rebuild trigger, dispatch, and run storage with pinned authority snapshots,
    # idempotency identities, and explicit organisation-scoped foreign keys.
    op.create_table(
        "automation_triggers",
        sa.Column(
            "trigger_id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False, server_default="agent"),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("target_revision", sa.Integer(), nullable=False),
        sa.Column("target_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("trigger_key", sa.Text(), nullable=False),
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("match_event_keys", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "config_schema_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "trigger_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column("watch_config", postgresql.JSONB, nullable=True),
        sa.Column("filter_config", postgresql.JSONB, nullable=True),
        sa.Column("execution_principal_type", sa.Text(), nullable=False),
        sa.Column("execution_principal_id", sa.UUID(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column(
            "scope_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column("audience_mode", sa.Text(), nullable=False),
        sa.Column(
            "audience_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column(
            "task_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column(
            "delivery_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("next_trigger_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "trigger_count",
            sa.BigInteger(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "target_type IN ('agent', 'workflow')",
            name="ck_automation_triggers_target_type",
        ),
        sa.CheckConstraint(
            "target_revision >= 1",
            name="ck_automation_triggers_target_revision",
        ),
        sa.CheckConstraint(
            "family IN ('event', 'manual', 'schedule', 'webhook')",
            name="ck_automation_triggers_family",
        ),
        sa.CheckConstraint(
            "cardinality(match_event_keys) >= 1",
            name="ck_automation_triggers_match_event_keys",
        ),
        sa.CheckConstraint(
            "config_schema_version >= 1",
            name="ck_automation_triggers_config_schema_version",
        ),
        sa.CheckConstraint(
            "(execution_principal_type = 'user' "
            "AND scope_type = 'owned_or_created_by_principal' "
            "AND audience_mode = 'principal') OR "
            "(execution_principal_type = 'organisation_service' "
            "AND execution_principal_id = organisation_id "
            "AND scope_type = 'organisation' "
            "AND audience_mode IN "
            "('none', 'explicit_users', 'subject_participants'))",
            name="ck_automation_triggers_principal_scope_audience",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'archived')",
            name="ck_automation_triggers_status",
        ),
        sa.CheckConstraint(
            "trigger_count >= 0 AND consecutive_failures >= 0",
            name="ck_automation_triggers_counts",
        ),
        sa.PrimaryKeyConstraint("trigger_id", name="pk_automation_triggers"),
        sa.UniqueConstraint(
            "organisation_id",
            "trigger_id",
            name="uq_automation_triggers_org_trigger_id",
        ),
    )
    op.create_index(
        "ix_automation_triggers_target",
        "automation_triggers",
        [
            "organisation_id",
            "created_by_user_id",
            "target_type",
            "target_id",
            "status",
            sa.text("updated_at DESC"),
        ],
        unique=False,
    )
    op.create_index(
        "ix_automation_triggers_due_schedule",
        "automation_triggers",
        ["next_trigger_at", "trigger_id"],
        unique=False,
        postgresql_where=sa.text(
            "status = 'active' AND family = 'schedule' AND next_trigger_at IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_automation_triggers_active_event_keys",
        "automation_triggers",
        ["match_event_keys"],
        unique=False,
        postgresql_using="gin",
        postgresql_where=sa.text("status = 'active' AND family = 'event'"),
    )
    op.create_index(
        "uq_automation_triggers_active_config",
        "automation_triggers",
        [
            "organisation_id",
            "created_by_user_id",
            "target_type",
            "target_id",
            "config_hash",
        ],
        unique=True,
        postgresql_where=sa.text("status <> 'archived'"),
    )
    op.create_index(
        "uq_automation_triggers_active_org_service_config",
        "automation_triggers",
        [
            "organisation_id",
            "target_type",
            "target_id",
            "config_hash",
        ],
        unique=True,
        postgresql_where=sa.text(
            "status <> 'archived' AND execution_principal_type = 'organisation_service'"
        ),
    )

    op.create_table(
        "automation_event_dispatches",
        sa.Column(
            "event_id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("route_mode", sa.Text(), nullable=False),
        sa.Column("direct_trigger_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("envelope", postgresql.JSONB, nullable=False),
        sa.Column(
            "activation_outcomes",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_ARRAY,
        ),
        sa.Column(
            "candidate_count",
            sa.Integer(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column(
            "matched_count",
            sa.Integer(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(route_mode = 'match' AND direct_trigger_id IS NULL) OR "
            "(route_mode = 'direct' AND direct_trigger_id IS NOT NULL)",
            name="ck_automation_event_dispatch_route",
        ),
        sa.CheckConstraint(
            "candidate_count >= 0 AND matched_count >= 0 AND matched_count <= candidate_count",
            name="ck_automation_event_dispatch_counts",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_automation_event_dispatch_attempts",
        ),
        sa.CheckConstraint(
            "NOT (processed_at IS NOT NULL AND failed_at IS NOT NULL)",
            name="ck_automation_event_dispatch_terminal_state",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id", "direct_trigger_id"],
            ["automation_triggers.organisation_id", "automation_triggers.trigger_id"],
            name="fk_automation_event_dispatches_direct_trigger_id_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "event_id",
            name="pk_automation_event_dispatches",
        ),
        sa.UniqueConstraint(
            "organisation_id",
            "event_id",
            name="uq_automation_event_dispatches_org_event_id",
        ),
    )
    op.create_index(
        "uq_automation_event_dispatch_idempotency",
        "automation_event_dispatches",
        ["organisation_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_automation_event_dispatch_direct_trigger",
        "automation_event_dispatches",
        ["organisation_id", "direct_trigger_id"],
        unique=False,
    )
    op.create_index(
        "ix_automation_event_dispatch_pending",
        "automation_event_dispatches",
        ["created_at", "event_id"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL AND failed_at IS NULL"),
    )
    op.create_index(
        "ix_automation_event_dispatch_event_created",
        "automation_event_dispatches",
        ["organisation_id", "event_key", sa.text("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "agent_runs",
        sa.Column(
            "run_id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=False),
        sa.Column("execution_principal_type", sa.Text(), nullable=False),
        sa.Column("execution_principal_id", sa.UUID(), nullable=False),
        sa.Column("audience_mode", sa.Text(), nullable=False),
        sa.Column(
            "audience_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("trigger_id", sa.UUID(), nullable=False),
        sa.Column("agent_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("caused_by_run_id", sa.UUID(), nullable=True),
        sa.Column(
            "causation_depth",
            sa.Integer(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column("trigger_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("agent_snapshot", postgresql.JSONB, nullable=False),
        sa.Column(
            "input_snapshot",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("output_json", postgresql.JSONB, nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("token_usage", postgresql.JSONB, nullable=True),
        sa.Column("cost", sa.Numeric(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(execution_principal_type = 'user' AND audience_mode = 'principal') OR "
            "(execution_principal_type = 'organisation_service' "
            "AND execution_principal_id = organisation_id "
            "AND audience_mode IN "
            "('none', 'explicit_users', 'subject_participants'))",
            name="ck_agent_runs_principal_audience",
        ),
        sa.CheckConstraint(
            "agent_revision >= 1",
            name="ck_agent_runs_agent_revision",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', "
            "'delivery_failed', 'validation_failed', 'cancelled')",
            name="ck_agent_runs_status",
        ),
        sa.CheckConstraint(
            "causation_depth BETWEEN 0 AND 3",
            name="ck_agent_runs_causation_depth",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_agent_runs_attempt_count",
        ),
        sa.CheckConstraint(
            "cost IS NULL OR cost >= 0",
            name="ck_agent_runs_cost",
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_agent_runs_latency_ms",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id", "agent_id"],
            ["user_agents.organisation_id", "user_agents.agent_id"],
            name="fk_agent_runs_agent_id_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id", "trigger_id"],
            ["automation_triggers.organisation_id", "automation_triggers.trigger_id"],
            name="fk_agent_runs_trigger_id_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id", "event_id"],
            [
                "automation_event_dispatches.organisation_id",
                "automation_event_dispatches.event_id",
            ],
            name="fk_agent_runs_event_id_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id", "caused_by_run_id"],
            ["agent_runs.organisation_id", "agent_runs.run_id"],
            name="fk_agent_runs_caused_by_run_id_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_agent_runs"),
        sa.UniqueConstraint(
            "organisation_id",
            "run_id",
            name="uq_agent_runs_org_run_id",
        ),
    )
    op.create_index(
        "ix_agent_runs_creator_created",
        "agent_runs",
        ["organisation_id", "created_by_user_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_principal_created",
        "agent_runs",
        [
            "organisation_id",
            "execution_principal_type",
            "execution_principal_id",
            sa.text("created_at DESC"),
        ],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_agent_created",
        "agent_runs",
        ["organisation_id", "agent_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_trigger_created",
        "agent_runs",
        ["organisation_id", "trigger_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_event",
        "agent_runs",
        ["organisation_id", "event_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_causation",
        "agent_runs",
        ["organisation_id", "caused_by_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_pending_claim",
        "agent_runs",
        ["created_at", "run_id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_agent_runs_trigger_event",
        "agent_runs",
        ["organisation_id", "trigger_id", "event_id"],
        unique=True,
    )
    op.create_foreign_key(
        "fk_crm_mutation_events_caused_by_run_id_org",
        "crm_mutation_events",
        "agent_runs",
        ["organisation_id", "caused_by_run_id"],
        ["organisation_id", "run_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_crm_mutation_events_causation",
        "crm_mutation_events",
        ["organisation_id", "caused_by_run_id"],
        unique=False,
    )

    # Apply the same forced tenant-RLS policy as other organisation tables only
    # after all new tables and their composite relationships exist.
    connection = op.get_bind()
    schema_name = _migration_schema()
    for table_name in (
        "automation_triggers",
        "automation_event_dispatches",
        "agent_runs",
    ):
        protect_tenant_table(
            connection,
            schema_name=schema_name,
            table_name=table_name,
        )


def downgrade() -> None:
    """Restore the revision-086 automation schema for the empty prototype tables."""

    # Downgrade has the same empty-table guard: restoring the prototype shape
    # must never delete live dispatches or runs.
    _assert_runtime_tables_empty()

    op.drop_constraint(
        "fk_crm_mutation_events_caused_by_run_id_org",
        "crm_mutation_events",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_crm_mutation_events_causation",
        table_name="crm_mutation_events",
    )

    # Remove the AI-native tables before recreating the legacy prototype shape;
    # the foreign key is dropped first so PostgreSQL can remove the run table.
    op.drop_table("agent_runs")
    op.drop_table("automation_event_dispatches")
    op.drop_table("automation_triggers")

    op.create_table(
        "automation_triggers",
        sa.Column(
            "trigger_id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "target_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'agent'"),
        ),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("target_revision", sa.Integer(), nullable=False),
        sa.Column("target_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("trigger_type", sa.Text(), nullable=False),
        sa.Column("source_domain", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column(
            "config_schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "trigger_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column(
            "filter_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column(
            "scope_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'owner'"),
        ),
        sa.Column(
            "scope_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column(
            "task_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column(
            "delivery_config",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("next_trigger_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "trigger_count",
            sa.BigInteger(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "target_type IN ('agent', 'workflow')",
            name="ck_automation_triggers_target_type",
        ),
        sa.CheckConstraint(
            "target_revision >= 1",
            name="ck_automation_triggers_target_revision",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('schedule', 'event')",
            name="ck_automation_triggers_trigger_type",
        ),
        sa.CheckConstraint(
            "config_schema_version >= 1",
            name="ck_automation_triggers_config_schema_version",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'archived')",
            name="ck_automation_triggers_status",
        ),
        sa.CheckConstraint(
            "trigger_count >= 0 AND consecutive_failures >= 0",
            name="ck_automation_triggers_counts",
        ),
        sa.PrimaryKeyConstraint("trigger_id"),
        sa.UniqueConstraint(
            "organisation_id",
            "trigger_id",
            name="uq_automation_triggers_org_trigger_id",
        ),
    )
    op.create_index(
        "ix_automation_triggers_target",
        "automation_triggers",
        [
            "organisation_id",
            "owner_user_id",
            "target_type",
            "target_id",
            "status",
            sa.text("updated_at DESC"),
        ],
    )
    op.create_index(
        "ix_automation_triggers_due_schedule",
        "automation_triggers",
        ["next_trigger_at", "trigger_id"],
        postgresql_where=sa.text(
            "status = 'active' AND trigger_type = 'schedule' AND next_trigger_at IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_automation_triggers_active_event",
        "automation_triggers",
        ["organisation_id", "source_domain", "event_type", "trigger_id"],
        postgresql_where=sa.text("status = 'active' AND trigger_type = 'event'"),
    )
    op.create_index(
        "uq_automation_triggers_active_config",
        "automation_triggers",
        [
            "organisation_id",
            "owner_user_id",
            "target_type",
            "target_id",
            "config_hash",
        ],
        unique=True,
        postgresql_where=sa.text("status <> 'archived'"),
    )

    op.create_table(
        "automation_event_dispatches",
        sa.Column(
            "dispatch_id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("source_domain", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_automation_event_dispatch_attempts",
        ),
        sa.CheckConstraint(
            "NOT (processed_at IS NOT NULL AND failed_at IS NOT NULL)",
            name="ck_automation_event_dispatch_terminal_state",
        ),
        sa.PrimaryKeyConstraint(
            "dispatch_id",
            name="pk_automation_event_dispatches",
        ),
    )
    op.create_index(
        "uq_automation_event_dispatch_source",
        "automation_event_dispatches",
        ["organisation_id", "source_domain", "source_event_id", "event_type"],
        unique=True,
    )
    op.create_index(
        "ix_automation_event_dispatch_pending",
        "automation_event_dispatches",
        ["created_at", "dispatch_id"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL AND failed_at IS NULL"),
    )

    op.create_table(
        "agent_runs",
        sa.Column(
            "run_id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("trigger_id", sa.UUID(), nullable=True),
        sa.Column("agent_revision", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("source_domain", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("source_event_id", sa.Text(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "trigger_snapshot",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column("agent_snapshot", postgresql.JSONB, nullable=False),
        sa.Column(
            "input_snapshot",
            postgresql.JSONB,
            nullable=False,
            server_default=_EMPTY_OBJECT,
        ),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("output_json", postgresql.JSONB, nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=_ZERO,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("token_usage", postgresql.JSONB, nullable=True),
        sa.Column("cost", sa.Numeric(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "agent_revision >= 1",
            name="ck_agent_runs_agent_revision",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'pending', 'running', 'succeeded', 'failed', 'delivery_failed', "
            "'validation_failed', 'cancelled'"
            ")",
            name="ck_agent_runs_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_agent_runs_attempt_count",
        ),
        sa.CheckConstraint(
            "cost IS NULL OR cost >= 0",
            name="ck_agent_runs_cost",
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_agent_runs_latency_ms",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id", "agent_id"],
            ["user_agents.organisation_id", "user_agents.agent_id"],
            name="fk_agent_runs_agent_id_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id", "trigger_id"],
            ["automation_triggers.organisation_id", "automation_triggers.trigger_id"],
            name="fk_agent_runs_trigger_id_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_agent_runs_owner_created",
        "agent_runs",
        ["organisation_id", "owner_user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_agent_runs_agent_created",
        "agent_runs",
        ["agent_id", "organisation_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_agent_runs_trigger_created",
        "agent_runs",
        ["trigger_id", "organisation_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_agent_runs_pending_claim",
        "agent_runs",
        ["created_at", "run_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_agent_runs_idempotency",
        "agent_runs",
        ["organisation_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.drop_constraint(
        "ck_crm_mutation_events_causation_depth",
        "crm_mutation_events",
        type_="check",
    )
    for column_name in (
        "causation_depth",
        "caused_by_run_id",
        "automation_source_mode",
        "record_snapshot",
    ):
        op.drop_column("crm_mutation_events", column_name)

    connection = op.get_bind()
    schema_name = _migration_schema()
    for table_name in (
        "automation_triggers",
        "automation_event_dispatches",
        "agent_runs",
    ):
        protect_tenant_table(
            connection,
            schema_name=schema_name,
            table_name=table_name,
        )
