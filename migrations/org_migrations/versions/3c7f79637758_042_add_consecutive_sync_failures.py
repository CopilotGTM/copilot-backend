"""042 add sync health columns to knowledge_integrations.

Adds consecutive_sync_failures (int) for circuit breaker logic — after 5
consecutive transient failures the sync pipeline skips the integration
until it naturally recovers.

Adds requires_reauth (bool) for permanent auth failures — set when
Google/Microsoft returns invalid_grant (revoked token, expired refresh,
password change, admin revocation). Frontend checks this to prompt the
user to re-connect. Only cleared when user re-authenticates.

Revision ID: 3c7f79637758
Revises: 3c7f79637759
Create Date: 2026-04-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637758"
down_revision: str | None = "3c7f79637759"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_integrations",
        sa.Column(
            "consecutive_sync_failures",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "knowledge_integrations",
        sa.Column(
            "requires_reauth",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_knowledge_integrations_requires_reauth",
        "knowledge_integrations",
        ["requires_reauth"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_integrations_requires_reauth",
        table_name="knowledge_integrations",
    )
    op.drop_column("knowledge_integrations", "requires_reauth")
    op.drop_column("knowledge_integrations", "consecutive_sync_failures")
