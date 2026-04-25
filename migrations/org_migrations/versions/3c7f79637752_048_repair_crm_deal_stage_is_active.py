"""048 repair crm_deal_stages.is_active for already-stamped org schemas.

Revision ID: 3c7f79637752
Revises: 3c7f79637753
Create Date: 2026-04-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7f79637752"
down_revision: str | None = "3c7f79637753"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    # Migration 046 was expanded after some dev schemas were stamped at 047.
    # Keep this forward repair idempotent so those schemas get the missing column.
    if not _has_column("crm_deal_stages", "is_active"):
        op.add_column(
            "crm_deal_stages",
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )

    op.create_index(
        "ix_crm_deal_stage_pipeline_active",
        "crm_deal_stages",
        ["pipeline_id", "is_active"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_crm_deal_stage_pipeline_active",
        table_name="crm_deal_stages",
        if_exists=True,
    )
    if _has_column("crm_deal_stages", "is_active"):
        op.drop_column("crm_deal_stages", "is_active")
