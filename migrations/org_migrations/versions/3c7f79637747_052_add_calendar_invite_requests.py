"""Add shared integration activity request audit table.

Revision ID: 3c7f79637747
Revises: 3c7f79637748
Create Date: 2026-05-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3c7f79637747"
down_revision: str | None = "3c7f79637748"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create one audit table for provider-side activity writes.

    Calendar invites and outbound emails both use this table. Shared fields are
    first-class columns; provider/tool-specific details live in JSON payload and
    history so future activity types do not need a new table each time.
    """
    op.create_table(
        "integration_activity_requests",
        sa.Column(
            "request_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("requested_by_user_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=True),
        sa.Column("linked_record_type", sa.String(length=64), nullable=True),
        sa.Column("linked_record_id", sa.UUID(), nullable=True),
        sa.Column("activity_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("title", sa.String(length=998), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(length=128), nullable=True),
        sa.Column("provider_timezone", sa.String(length=128), nullable=True),
        sa.Column(
            "participants",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("provider_resource_id", sa.String(length=255), nullable=True),
        sa.Column("conference_link", sa.Text(), nullable=True),
        sa.Column(
            "request_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("provider_status_code", sa.Integer(), nullable=True),
        sa.Column("provider_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
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
            "status IN ('PENDING','SENDING','SENT','FAILED')",
            name="ck_integration_activity_request_status",
        ),
        sa.UniqueConstraint(
            "organisation_id",
            "requested_by_user_id",
            "activity_type",
            "idempotency_key",
            name="uq_integration_activity_request_idempotency",
        ),
        sa.ForeignKeyConstraint(["integration_id"], ["knowledge_integrations.integration_id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["crm_deals.deal_id"]),
    )

    op.create_index(
        "ix_integration_activity_requests_organisation_id",
        "integration_activity_requests",
        ["organisation_id"],
    )
    op.create_index(
        "ix_integration_activity_requests_requested_by_user_id",
        "integration_activity_requests",
        ["requested_by_user_id"],
    )
    op.create_index(
        "ix_integration_activity_requests_integration_id",
        "integration_activity_requests",
        ["integration_id"],
    )
    op.create_index(
        "ix_integration_activity_requests_deal_id",
        "integration_activity_requests",
        ["deal_id"],
    )
    op.create_index(
        "ix_integration_activity_requests_linked_record",
        "integration_activity_requests",
        ["linked_record_type", "linked_record_id"],
    )
    op.create_index(
        "ix_integration_activity_requests_provider_resource_id",
        "integration_activity_requests",
        ["provider_resource_id"],
    )
    op.create_index(
        "ix_integration_activity_request_org_status",
        "integration_activity_requests",
        ["organisation_id", "status"],
    )
    op.create_index(
        "ix_integration_activity_request_type_status",
        "integration_activity_requests",
        ["activity_type", "status"],
    )


def downgrade() -> None:
    """Drop the shared provider activity request table."""
    op.drop_index(
        "ix_integration_activity_request_type_status",
        table_name="integration_activity_requests",
    )
    op.drop_index(
        "ix_integration_activity_request_org_status",
        table_name="integration_activity_requests",
    )
    op.drop_index(
        "ix_integration_activity_requests_provider_resource_id",
        table_name="integration_activity_requests",
    )
    op.drop_index(
        "ix_integration_activity_requests_linked_record",
        table_name="integration_activity_requests",
    )
    op.drop_index(
        "ix_integration_activity_requests_deal_id",
        table_name="integration_activity_requests",
    )
    op.drop_index(
        "ix_integration_activity_requests_integration_id",
        table_name="integration_activity_requests",
    )
    op.drop_index(
        "ix_integration_activity_requests_requested_by_user_id",
        table_name="integration_activity_requests",
    )
    op.drop_index(
        "ix_integration_activity_requests_organisation_id",
        table_name="integration_activity_requests",
    )
    op.drop_table("integration_activity_requests")
