"""Add CRM CSV import row ledger.

Revision ID: 3c7f79637725
Revises: 3c7f79637726
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637725"
down_revision: str | None = "3c7f79637726"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crm_import_rows",
        sa.Column("row_id", sa.Uuid(), nullable=False),
        sa.Column("import_job_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw_values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "normalized_values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("deal_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["import_job_id"], ["deal_jobs.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("row_id"),
    )
    op.create_index(
        "ux_crm_import_rows_import_row_number",
        "crm_import_rows",
        ["organisation_id", "import_job_id", "row_number"],
        unique=True,
    )
    op.create_index(
        "ix_crm_import_rows_import_status",
        "crm_import_rows",
        ["organisation_id", "import_job_id", "status"],
    )
    op.create_index(
        "ix_crm_import_rows_org_status",
        "crm_import_rows",
        ["organisation_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_crm_import_rows_org_status", table_name="crm_import_rows")
    op.drop_index("ix_crm_import_rows_import_status", table_name="crm_import_rows")
    op.drop_index("ux_crm_import_rows_import_row_number", table_name="crm_import_rows")
    op.drop_table("crm_import_rows")
