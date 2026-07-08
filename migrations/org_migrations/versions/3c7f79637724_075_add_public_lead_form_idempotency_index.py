"""075 add public lead form idempotency index.

Revision ID: 3c7f79637724
Revises: 3c7f79637725
Create Date: 2026-07-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637724"
down_revision: str | None = "3c7f79637725"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_lead_form_submission_public_idempotency",
        "lead_form_submissions",
        ["organisation_id", "form_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("ingestion_key_id IS NULL AND idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_lead_form_submission_public_idempotency",
        table_name="lead_form_submissions",
    )
