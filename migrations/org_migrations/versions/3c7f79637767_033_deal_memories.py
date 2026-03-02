"""033 add deal_memories table.

Revision ID: 3c7f79637767
Revises: 3c7f79637768
Create Date: 2026-03-02 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637767"
down_revision: str | None = "3c7f79637768"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deal_memories",
        sa.Column(
            "memory_id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organisation_id", sa.UUID(), nullable=False, index=True),
        sa.Column("deal_id", sa.UUID(), nullable=False, index=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("token_count", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["deal_id"], ["crm_deals.deal_id"], name="fk_deal_memories_deal"
        ),
    )
    op.create_unique_constraint(
        "uq_deal_memories_org_deal", "deal_memories", ["organisation_id", "deal_id"]
    )
    op.create_index("ix_deal_memories_org", "deal_memories", ["organisation_id"])


def downgrade() -> None:
    op.drop_index("ix_deal_memories_org", table_name="deal_memories")
    op.drop_constraint("uq_deal_memories_org_deal", "deal_memories", type_="unique")
    op.drop_table("deal_memories")
