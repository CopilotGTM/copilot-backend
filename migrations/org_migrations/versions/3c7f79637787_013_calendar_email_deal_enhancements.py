"""013 calendar deduplication, email enhancements, and deal email associations

Revision ID: 3c7f79637787
Revises: 3c7f79637788
Create Date: 2025-12-04 10:00:00.000000

This migration:
1. Calendar Event Deduplication:
   - Changes unique constraint from (integration_id, external_id) to (organisation_id, external_id)
   - Makes integration_id nullable (events can be shared across integrations)
   - Creates calendar_event_user_states table to track which integrations synced each event

2. Email Enhancements:
   - Adds internet_message_id column to emails table for cross-user deduplication
   - Creates composite index on (organisation_id, internet_message_id)

3. Deal Email Associations:
   - Creates deal_email_associations table for linking emails to deals
   - Uses internet_message_id for cross-user deduplication

4. Stakeholder Source:
   - Adds EMAIL_DISCOVERY to stakeholdersource enum

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "3c7f79637787"
down_revision: Union[str, None] = "3c7f79637788"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ========================================
    # 1. Calendar Event Deduplication
    # ========================================

    # 1a. Drop the old unique constraint (integration_id, external_id)
    op.drop_constraint("uq_calendar_event_external", "calendar_events", type_="unique")

    # 1b. Create new unique constraint (organisation_id, external_id)
    op.create_unique_constraint(
        "uq_calendar_event_external",
        "calendar_events",
        ["organisation_id", "external_id"],
    )

    # 1c. Make integration_id nullable
    # First drop the foreign key constraint
    op.drop_constraint(
        "calendar_events_integration_id_fkey", "calendar_events", type_="foreignkey"
    )

    # Alter column to be nullable
    op.alter_column(
        "calendar_events",
        "integration_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )

    # Re-add foreign key constraint (allowing NULL)
    op.create_foreign_key(
        "calendar_events_integration_id_fkey",
        "calendar_events",
        "knowledge_integrations",
        ["integration_id"],
        ["integration_id"],
    )

    # 1d. Create calendar_event_user_states table
    op.create_table(
        "calendar_event_user_states",
        sa.Column("state_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["calendar_events.event_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["knowledge_integrations.integration_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("state_id"),
        sa.UniqueConstraint("event_id", "integration_id", name="uq_calendar_event_user_state"),
    )

    op.create_index(
        "ix_calendar_user_state_integration",
        "calendar_event_user_states",
        ["integration_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_calendar_event_user_states_event_id"),
        "calendar_event_user_states",
        ["event_id"],
        unique=False,
    )

    # ========================================
    # 2. Email internet_message_id Column
    # ========================================

    # 2a. Add internet_message_id column to emails table
    op.add_column(
        "emails",
        sa.Column(
            "internet_message_id",
            sa.String(length=512),
            nullable=True,
            comment="RFC 822 Message-ID header. Same across all recipients - use for cross-user deduplication and deal mapping.",
        ),
    )

    # 2b. Create composite index for efficient lookups
    op.create_index(
        "ix_email_internet_message_id",
        "emails",
        ["organisation_id", "internet_message_id"],
        unique=False,
    )

    # ========================================
    # 3. Stakeholder Source Enum Update
    # ========================================

    # Add EMAIL_DISCOVERY to stakeholdersource enum
    op.execute("ALTER TYPE stakeholdersource ADD VALUE IF NOT EXISTS 'EMAIL_DISCOVERY'")

    # ========================================
    # 4. Deal Email Associations Table
    # ========================================

    op.create_table(
        "deal_email_associations",
        sa.Column("association_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organisation_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "deal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("crm_deals.deal_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "internet_message_id",
            sa.String(512),
            nullable=False,
            comment="RFC 822 Message-ID - same across all recipients of the email.",
        ),
        # Matching metadata
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="SUGGESTED",
            comment="Association status: SUGGESTED, CONFIRMED, or REJECTED.",
        ),
        sa.Column(
            "match_score",
            sa.Float,
            nullable=False,
            server_default="0.0",
            comment="Confidence score 0.0-1.0 based on stakeholder matching.",
        ),
        # Email-specific metadata
        sa.Column(
            "direction",
            sa.String(32),
            nullable=False,
            server_default="INBOUND",
            comment="Direction relative to deal team: INBOUND, OUTBOUND, or INTERNAL.",
        ),
        sa.Column(
            "matched_stakeholder_emails",
            JSONB,
            nullable=True,
            comment="Which stakeholder emails matched this email for audit trail.",
        ),
        # Email context (denormalized)
        sa.Column(
            "email_subject",
            sa.String(500),
            nullable=True,
            comment="Email subject line for display purposes.",
        ),
        sa.Column(
            "email_date",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the email was sent/received.",
        ),
        # Timestamps
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
    )

    # Create indexes
    op.create_index(
        "ix_deal_email_org_status",
        "deal_email_associations",
        ["organisation_id", "status"],
    )
    op.create_index(
        "ix_deal_email_deal",
        "deal_email_associations",
        ["deal_id"],
    )
    op.create_index(
        "ix_deal_email_internet_msg",
        "deal_email_associations",
        ["internet_message_id"],
    )

    # Create unique constraint
    op.create_unique_constraint(
        "uq_deal_email",
        "deal_email_associations",
        ["deal_id", "internet_message_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""

    # ========================================
    # 1. Drop deal_email_associations table
    # ========================================
    op.drop_table("deal_email_associations")

    # Note: Cannot remove enum value in PostgreSQL, so we skip removing EMAIL_DISCOVERY

    # ========================================
    # 2. Remove Email internet_message_id
    # ========================================
    op.drop_index("ix_email_internet_message_id", table_name="emails")
    op.drop_column("emails", "internet_message_id")

    # ========================================
    # 3. Remove calendar_event_user_states table
    # ========================================
    op.drop_index(
        op.f("ix_calendar_event_user_states_event_id"), table_name="calendar_event_user_states"
    )
    op.drop_index("ix_calendar_user_state_integration", table_name="calendar_event_user_states")
    op.drop_table("calendar_event_user_states")

    # ========================================
    # 4. Revert Calendar Event changes
    # ========================================

    # 4a. Drop the foreign key constraint
    op.drop_constraint(
        "calendar_events_integration_id_fkey", "calendar_events", type_="foreignkey"
    )

    # 4b. Make integration_id NOT NULL again (may fail if there are NULL values)
    op.alter_column(
        "calendar_events",
        "integration_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )

    # 4c. Re-add foreign key constraint
    op.create_foreign_key(
        "calendar_events_integration_id_fkey",
        "calendar_events",
        "knowledge_integrations",
        ["integration_id"],
        ["integration_id"],
    )

    # 4d. Drop the new unique constraint (organisation_id, external_id)
    op.drop_constraint("uq_calendar_event_external", "calendar_events", type_="unique")

    # 4e. Create old unique constraint (integration_id, external_id)
    op.create_unique_constraint(
        "uq_calendar_event_external",
        "calendar_events",
        ["integration_id", "external_id"],
    )
