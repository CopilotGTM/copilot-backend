"""Add Gmail routing email to the public integration locator.

Revision ID: f1a2b3c4d5ed
Revises: f1a2b3c4d5ec
Create Date: 2026-07-13
"""

import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5ed"
down_revision = "f1a2b3c4d5ec"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a nullable routing key so deployment can precede the backfill."""
    op.add_column(
        "organisation_integration_details",
        sa.Column("integration_email", sa.String(length=255), nullable=True),
        schema="public",
    )
    op.create_index(
        "ix_organisation_integration_details_integration_email",
        "organisation_integration_details",
        ["integration_email"],
        unique=False,
        schema="public",
    )


def downgrade() -> None:
    """Remove the Gmail routing index and key."""
    op.drop_index(
        "ix_organisation_integration_details_integration_email",
        table_name="organisation_integration_details",
        schema="public",
    )
    op.drop_column(
        "organisation_integration_details",
        "integration_email",
        schema="public",
    )
