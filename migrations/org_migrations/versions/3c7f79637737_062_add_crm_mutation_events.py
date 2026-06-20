"""Add the tenant-scoped CRM mutation audit table.

The table is canonical for internal CRM write history. Workflow dispatch
claim/retry/run state belongs in future workflow runtime tables because one
mutation can fan out to multiple workflow runs.

Revision ID: 3c7f79637737
Revises: 3c7f79637738
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637737"
down_revision: str | None = "3c7f79637738"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the durable CRM mutation ledger used by post-write workflows."""
    # The row is immutable audit truth once inserted. Runtime dispatch state is
    # intentionally excluded so a future child table can track per-workflow fanout.
    op.create_table(
        "crm_mutation_events",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column(
            "semantic_event_types",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("ARRAY[]::text[]"),
        ),
        sa.Column("mutation_group_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "changed_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("summary", sa.String(length=500), nullable=True),
        sa.Column("integration_id", sa.UUID(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("source_object_mapping_id", sa.UUID(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_label", sa.String(length=128), nullable=True),
        sa.Column("approval_state_id", sa.UUID(), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["deal_id"], ["crm_deals.deal_id"]),
        sa.PrimaryKeyConstraint("event_id"),
    )
    # One multi-field mutation can expose multiple trigger semantics.
    op.create_index(
        "ix_crm_mutation_events_semantic_event_types",
        "crm_mutation_events",
        ["semantic_event_types"],
        postgresql_using="gin",
    )
    # Generic entity history lookup, used when future CRM entities share this ledger.
    op.create_index(
        "ix_crm_mutation_events_entity",
        "crm_mutation_events",
        ["organisation_id", "entity_type", "entity_id", "created_at"],
    )
    # Deal timeline screens read by org/deal and sort by the business occurrence time.
    op.create_index(
        "ix_crm_mutation_events_deal",
        "crm_mutation_events",
        ["organisation_id", "deal_id", "occurred_at"],
    )
    # Idempotency is tenant-local so integrations or agents can safely retry writes.
    op.create_index(
        "ux_crm_mutation_events_idempotency",
        "crm_mutation_events",
        ["organisation_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_crm_mutation_events_organisation_id",
        "crm_mutation_events",
        ["organisation_id"],
    )
    op.create_index("ix_crm_mutation_events_event_type", "crm_mutation_events", ["event_type"])
    op.create_index(
        "ix_crm_mutation_events_mutation_group_id",
        "crm_mutation_events",
        ["mutation_group_id"],
    )
    op.create_index("ix_crm_mutation_events_entity_id", "crm_mutation_events", ["entity_id"])
    op.create_index("ix_crm_mutation_events_deal_id", "crm_mutation_events", ["deal_id"])
    op.create_index("ix_crm_mutation_events_operation", "crm_mutation_events", ["operation"])
    op.create_index("ix_crm_mutation_events_integration_id", "crm_mutation_events", ["integration_id"])
    op.create_index(
        "ix_crm_mutation_events_source_object_mapping_id",
        "crm_mutation_events",
        ["source_object_mapping_id"],
    )
    op.create_index("ix_crm_mutation_events_actor_user_id", "crm_mutation_events", ["actor_user_id"])
    op.create_index("ix_crm_mutation_events_source", "crm_mutation_events", ["source"])
    op.create_index(
        "ix_crm_mutation_events_approval_state_id",
        "crm_mutation_events",
        ["approval_state_id"],
    )
    op.create_index("ix_crm_mutation_events_occurred_at", "crm_mutation_events", ["occurred_at"])
    op.create_index("ix_crm_mutation_events_created_at", "crm_mutation_events", ["created_at"])
    op.create_index("ix_crm_mutation_events_updated_at", "crm_mutation_events", ["updated_at"])


def downgrade() -> None:
    """Drop the CRM mutation outbox."""
    # Downgrade removes history/outbox data; only use before this migration reaches shared environments.
    op.drop_index("ix_crm_mutation_events_updated_at", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_created_at", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_occurred_at", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_approval_state_id", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_source", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_actor_user_id", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_source_object_mapping_id", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_integration_id", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_operation", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_deal_id", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_entity_id", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_mutation_group_id", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_event_type", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_organisation_id", table_name="crm_mutation_events")
    op.drop_index("ux_crm_mutation_events_idempotency", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_deal", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_entity", table_name="crm_mutation_events")
    op.drop_index("ix_crm_mutation_events_semantic_event_types", table_name="crm_mutation_events")
    op.drop_table("crm_mutation_events")
