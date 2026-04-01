"""041 add created_by_user_id to deal_stakeholders.

Tracks which user added the stakeholder. NULL for legacy records,
AUTOMATED_PIPELINE_USER_ID for automated sources (CRM sync, discovery).

Revision ID: 3c7f79637759
Revises: 3c7f79637760
Create Date: 2026-03-31
"""

from alembic import op
import sqlalchemy as sa

revision = "3c7f79637759"
down_revision = "3c7f79637760"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deal_stakeholders",
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_deal_stakeholder_created_by",
        "deal_stakeholders",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_deal_stakeholder_created_by", table_name="deal_stakeholders")
    op.drop_column("deal_stakeholders", "created_by_user_id")
