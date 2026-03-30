"""040 add stakeholder research columns.

Adds stakeholder_research (text) and last_researched_at (timestamptz)
to deal_stakeholders for persisting LinkedIn/Apollo research profiles.

Revision ID: 3c7f79637760
Revises: 3c7f79637761
Create Date: 2026-03-30
"""

from alembic import op
import sqlalchemy as sa

revision = "3c7f79637760"
down_revision = "3c7f79637761"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deal_stakeholders",
        sa.Column("stakeholder_research", sa.Text(), nullable=True),
    )
    op.add_column(
        "deal_stakeholders",
        sa.Column(
            "last_researched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("deal_stakeholders", "last_researched_at")
    op.drop_column("deal_stakeholders", "stakeholder_research")
