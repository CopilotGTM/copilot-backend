"""Add user agents, generic automation triggers, and durable agent runs.

Revision ID: 3c7f79637721
Revises: 3c7f79637722
Create Date: 2026-07-12

The three tables deliberately have separate lifecycles. Triggers pin executable snapshots,
and workers claim run rows instead of changing trigger lifecycle state during execution.

Only stable relational invariants and initial query paths are enforced here. Services own
evolving JSON shapes, catalog validation, and lifecycle policy.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637721"
down_revision: str | None = "3c7f79637722"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EXPECTED_COLUMNS = {
    "user_agents": {
        "agent_id",
        "organisation_id",
        "owner_user_id",
        "source_type",
        "source_agent_id",
        "source_version",
        "revision",
        "visibility",
        "status",
        "agent_type",
        "name",
        "description",
        "prompt",
        "model_name",
        "model_config",
        "allowed_tools",
        "allowed_skills",
        "max_turns",
        "input_schema",
        "output_mode",
        "output_schema",
        "created_at",
        "updated_at",
    },
    "automation_triggers": {
        "trigger_id",
        "organisation_id",
        "owner_user_id",
        "name",
        "target_type",
        "target_id",
        "target_revision",
        "target_snapshot",
        "trigger_type",
        "source_domain",
        "event_type",
        "config_schema_version",
        "trigger_config",
        "filter_config",
        "scope_type",
        "scope_config",
        "task_config",
        "delivery_config",
        "config_hash",
        "status",
        "next_trigger_at",
        "last_triggered_at",
        "trigger_count",
        "consecutive_failures",
        "last_error",
        "created_at",
        "updated_at",
    },
    "agent_runs": {
        "run_id",
        "organisation_id",
        "owner_user_id",
        "agent_id",
        "trigger_id",
        "agent_revision",
        "status",
        "idempotency_key",
        "source_domain",
        "event_type",
        "source_event_id",
        "scheduled_for",
        "trigger_snapshot",
        "agent_snapshot",
        "input_snapshot",
        "output_text",
        "output_json",
        "error_code",
        "error_message",
        "attempt_count",
        "claimed_at",
        "claimed_by",
        "model_name",
        "token_usage",
        "cost",
        "latency_ms",
        "created_at",
        "started_at",
        "completed_at",
    },
}


def _reconcile_preexisting_schema() -> bool:
    """Upgrade the retired workflow agent table or accept a fully provisioned schema."""
    inspector = sa.inspect(op.get_bind())
    present = set(_EXPECTED_COLUMNS).intersection(inspector.get_table_names())
    if not present:
        return False

    if present == set(_EXPECTED_COLUMNS):
        missing = {
            table: expected
            - {column["name"] for column in inspector.get_columns(table)}
            for table, expected in _EXPECTED_COLUMNS.items()
        }
        missing = {table: columns for table, columns in missing.items() if columns}
        if missing:
            raise RuntimeError(
                f"Existing agent automation tables do not match revision 078: {missing}"
            )
        return True

    if present != {"user_agents"}:
        raise RuntimeError(
            "Existing agent automation tables do not match revision 078: "
            f"partial tables {sorted(present)}"
        )

    columns = {column["name"] for column in inspector.get_columns("user_agents")}
    legacy_columns = _EXPECTED_COLUMNS["user_agents"] - {
        "source_type",
        "source_agent_id",
        "source_version",
        "revision",
        "visibility",
    }
    if not legacy_columns.issubset(columns):
        raise RuntimeError(
            "Existing agent automation tables do not match revision 078: "
            f"user_agents is missing {sorted(legacy_columns - columns)}"
        )

    # copilotgtm: A retired workflow migration created this table without provenance.
    # Keep its foreign keys and rows intact; remove this bridge after all tenants reach 078.
    new_columns: dict[str, sa.Column[Any]] = {
        "source_type": sa.Column(
            "source_type", sa.Text(), nullable=False, server_default=sa.text("'user'")
        ),
        "source_agent_id": sa.Column("source_agent_id", sa.UUID(), nullable=True),
        "source_version": sa.Column("source_version", sa.Integer(), nullable=True),
        "revision": sa.Column(
            "revision", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        "visibility": sa.Column(
            "visibility", sa.Text(), nullable=False, server_default=sa.text("'private'")
        ),
    }
    for name, column in new_columns.items():
        if name not in columns:
            op.add_column("user_agents", column)

    existing_checks = {
        constraint["name"] for constraint in inspector.get_check_constraints("user_agents")
    }
    checks = {
        "ck_user_agents_revision": "revision >= 1",
        "ck_user_agents_source_type": "source_type IN ('user', 'system')",
        "ck_user_agents_visibility": "visibility IN ('private', 'organisation')",
        "ck_user_agents_status": "status IN ('active', 'paused', 'archived')",
        "ck_user_agents_output_mode": "output_mode IN ('text', 'json')",
    }
    for name, condition in checks.items():
        if name not in existing_checks:
            op.create_check_constraint(name, "user_agents", condition)

    existing_indexes = {index["name"] for index in inspector.get_indexes("user_agents")}
    for name in (
        "ix_user_agents_organisation_id",
        "ix_user_agents_owner_user_id",
        "ix_user_agents_org_owner_status",
    ):
        if name in existing_indexes:
            op.drop_index(name, table_name="user_agents")
    return False


def upgrade() -> None:
    """Create agent definitions, reusable triggers, and immutable run history."""
    if _reconcile_preexisting_schema():
        return

    op.create_table(
        "user_agents",
        sa.Column(
            "agent_id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column(
            "source_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'user'"),
        ),
        sa.Column("source_agent_id", sa.UUID(), nullable=True),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "visibility",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'private'"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "agent_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'general'"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column(
            "model_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "allowed_tools",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "allowed_skills",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("max_turns", sa.Integer(), nullable=True),
        sa.Column(
            "input_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "output_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'text'"),
        ),
        sa.Column(
            "output_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
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
            "revision >= 1",
            name="ck_user_agents_revision",
        ),
        sa.CheckConstraint(
            "source_type IN ('user', 'system')",
            name="ck_user_agents_source_type",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'organisation')",
            name="ck_user_agents_visibility",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'archived')",
            name="ck_user_agents_status",
        ),
        sa.CheckConstraint(
            "output_mode IN ('text', 'json')",
            name="ck_user_agents_output_mode",
        ),
        sa.PrimaryKeyConstraint("agent_id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_user_agents_owner_status_updated",
        "user_agents",
        [
            "organisation_id",
            "owner_user_id",
            "status",
            sa.text("updated_at DESC"),
        ],
        if_not_exists=True,
    )

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
        # A conditional foreign key cannot target agents now and workflows later.
        # Target resolvers enforce tenant, owner, lifecycle, and revision integrity.
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("target_revision", sa.Integer(), nullable=False),
        sa.Column(
            "target_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
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
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "filter_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "scope_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'owner'"),
        ),
        sa.Column(
            "scope_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "task_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "delivery_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "next_trigger_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_triggered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "trigger_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
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
        sa.Column(
            "scheduled_for",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "trigger_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "agent_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "input_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column(
            "output_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column(
            "token_usage",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("cost", sa.Numeric(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
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
            ["agent_id"],
            ["user_agents.agent_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_id"],
            ["automation_triggers.trigger_id"],
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


def downgrade() -> None:
    """Drop run history first, then its trigger and agent parents."""
    op.drop_index(
        "uq_agent_runs_idempotency",
        table_name="agent_runs",
    )
    op.drop_index(
        "ix_agent_runs_pending_claim",
        table_name="agent_runs",
    )
    op.drop_index(
        "ix_agent_runs_trigger_created",
        table_name="agent_runs",
    )
    op.drop_index(
        "ix_agent_runs_agent_created",
        table_name="agent_runs",
    )
    op.drop_index(
        "ix_agent_runs_owner_created",
        table_name="agent_runs",
    )
    op.drop_table("agent_runs")

    op.drop_index(
        "uq_automation_triggers_active_config",
        table_name="automation_triggers",
    )
    op.drop_index(
        "ix_automation_triggers_active_event",
        table_name="automation_triggers",
    )
    op.drop_index(
        "ix_automation_triggers_due_schedule",
        table_name="automation_triggers",
    )
    op.drop_index(
        "ix_automation_triggers_target",
        table_name="automation_triggers",
    )
    op.drop_table("automation_triggers")

    op.drop_index(
        "ix_user_agents_owner_status_updated",
        table_name="user_agents",
    )
    op.drop_table("user_agents")
