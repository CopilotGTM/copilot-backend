"""007_crm_deal_enhancements

Revision ID: c0d9b8d2c7f1
Revises: 3c7f79637794
Create Date: 2025-10-20 09:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "c0d9b8d2c7f1"
down_revision: Union[str, None] = "3c7f79637794"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "crm_deals",
        sa.Column("pipeline_external_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "crm_deals",
        sa.Column("pipeline_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column(
        "crm_deals",
        sa.Column("pipeline_label", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column(
        "crm_deals",
        sa.Column("stage_external_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "crm_deals",
        sa.Column("stage_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column(
        "crm_deals",
        sa.Column("stage_label", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column(
        "crm_deals",
        sa.Column(
            "associations_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_crm_deal_pipeline_external",
        "crm_deals",
        ["pipeline_external_id"],
        unique=False,
    )
    op.create_index(
        "ix_crm_deal_stage_external",
        "crm_deals",
        ["stage_external_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_crm_deal_stage_external", table_name="crm_deals")
    op.drop_index("ix_crm_deal_pipeline_external", table_name="crm_deals")

    op.drop_column("crm_deals", "associations_payload")
    op.drop_column("crm_deals", "stage_label")
    op.drop_column("crm_deals", "stage_name")
    op.drop_column("crm_deals", "stage_external_id")
    op.drop_column("crm_deals", "pipeline_label")
    op.drop_column("crm_deals", "pipeline_name")
    op.drop_column("crm_deals", "pipeline_external_id")

