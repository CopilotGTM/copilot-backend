"""Add a CRM user normalized email uniqueness guard.

Revision ID: 3c7f79637728
Revises: 3c7f79637729
Create Date: 2026-07-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "3c7f79637728"
down_revision: str | None = "3c7f79637729"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_crm_users_org_normalized_email
            ON crm_users (organisation_id, lower(btrim(email)))
            WHERE email IS NOT NULL AND btrim(email) <> ''
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_crm_users_org_normalized_email")
