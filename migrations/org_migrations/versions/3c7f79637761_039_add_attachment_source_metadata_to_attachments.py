"""039 add attachment source metadata to attachments.

This revision intentionally adds only provider provenance fields. The earlier
derived-text columns were removed from the design before rollout, so they are
not part of this migration.

Revision ID: 3c7f79637761
Revises: 3c7f79637762
Create Date: 2026-03-27
"""

from alembic import op
import sqlalchemy as sa


revision = "3c7f79637761"
down_revision = "3c7f79637762"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep provider provenance on the attachment row so web, Slack, and Teams
    # share the same storage model without introducing a second attachment table.
    op.add_column(
        "attachments",
        sa.Column(
            "source_provider",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'web'"),
        ),
    )
    op.add_column(
        "attachments",
        sa.Column("source_external_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attachments", "source_external_id")
    op.drop_column("attachments", "source_provider")
