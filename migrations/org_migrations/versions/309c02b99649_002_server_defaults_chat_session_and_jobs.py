"""server_defaults_chat_session_and_jobs

Revision ID: 309c02b99649
Revises: f342cceaaaa8
Create Date: 2025-08-31 01:15:59.359156

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "309c02b99649"
down_revision: Union[str, None] = "f342cceaaaa8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Hardened migration: staged changes with backfills & conditional adds
    conn = op.get_bind()

    # 1. chat_messages: ensure agent_data column exists before dropping old columns
    inspector = sa.inspect(conn)
    chat_cols = {c["name"] for c in inspector.get_columns("chat_messages")}
    if "agent_data" not in chat_cols:
        op.add_column(
            "chat_messages",
            sa.Column(
                "agent_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True
            ),
        )

    # Drop legacy columns only if present (idempotent safety)
    legacy_cols = ["rfp_details", "technical_deck_details"]
    for legacy in legacy_cols:
        if legacy in chat_cols:
            op.drop_column("chat_messages", legacy)

    # 2. organisation_sources: add is_deleted if missing
    org_src_cols = {c["name"] for c in inspector.get_columns("organisation_sources")}
    if "is_deleted" not in org_src_cols:
        op.add_column(
            "organisation_sources",
            sa.Column(
                "is_deleted",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=True,
            ),
        )
        # backfill nulls now (all rows get false)
        conn.execute(
            sa.text(
                "UPDATE organisation_sources SET is_deleted = false WHERE is_deleted IS NULL"
            )
        )
        # make NOT NULL and create index
        op.alter_column(
            "organisation_sources",
            "is_deleted",
            nullable=False,
            existing_type=sa.Boolean(),
        )
        op.create_index(
            op.f("ix_organisation_sources_is_deleted"),
            "organisation_sources",
            ["is_deleted"],
            unique=False,
        )

    # 3. Backfill any NULL status/source_type before enforcing NOT NULL
    conn.execute(
        sa.text(
            "UPDATE organisation_sources SET source_type = 'file' WHERE source_type IS NULL"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE organisation_sources SET status = 'pending_upload' WHERE status IS NULL"
        )
    )

    # 4. Enforce NOT NULL constraints (idempotent, will no-op if already set)
    op.alter_column(
        "organisation_sources",
        "source_type",
        existing_type=sa.VARCHAR(length=50),
        nullable=False,
    )
    op.alter_column(
        "organisation_sources",
        "status",
        existing_type=sa.VARCHAR(length=50),
        nullable=False,
    )

    # 5. Other tables simple NOT NULL enforcement (no backfill needed as ORM always set defaults)
    op.alter_column(
        "attachments", "status", existing_type=sa.VARCHAR(length=50), nullable=False
    )
    op.alter_column(
        "chat_message_jobs",
        "status",
        existing_type=sa.VARCHAR(length=50),
        nullable=False,
    )
    op.alter_column(
        "chat_messages", "role", existing_type=sa.VARCHAR(length=50), nullable=False
    )
    op.alter_column(
        "chat_messages",
        "message_type",
        existing_type=sa.VARCHAR(length=50),
        nullable=False,
    )
    op.alter_column(
        "chat_sessions",
        "chat_type",
        existing_type=sa.VARCHAR(length=50),
        nullable=False,
    )
    op.alter_column(
        "knowledge_processing_jobs",
        "status",
        existing_type=sa.VARCHAR(length=50),
        nullable=False,
    )

    # NOTE: server defaults themselves are captured from models; this migration focuses on data safety.


def downgrade() -> None:
    """Downgrade schema."""
    # Downgrade attempts to restore nullable state & legacy columns (best‑effort)
    inspector = sa.inspect(op.get_bind())
    org_src_cols = {c["name"] for c in inspector.get_columns("organisation_sources")}
    if "ix_organisation_sources_is_deleted" in [
        ix["name"] for ix in inspector.get_indexes("organisation_sources")
    ]:
        op.drop_index(
            op.f("ix_organisation_sources_is_deleted"),
            table_name="organisation_sources",
        )
    if "is_deleted" in org_src_cols:
        op.drop_column("organisation_sources", "is_deleted")
    # Relax NOT NULLs back to nullable
    op.alter_column(
        "organisation_sources",
        "status",
        existing_type=sa.VARCHAR(length=50),
        nullable=True,
    )
    op.alter_column(
        "organisation_sources",
        "source_type",
        existing_type=sa.VARCHAR(length=50),
        nullable=True,
    )
    op.alter_column(
        "knowledge_processing_jobs",
        "status",
        existing_type=sa.VARCHAR(length=50),
        nullable=True,
    )
    op.alter_column(
        "chat_sessions", "chat_type", existing_type=sa.VARCHAR(length=50), nullable=True
    )
    # Restore legacy chat_messages columns if absent
    chat_cols = {c["name"] for c in inspector.get_columns("chat_messages")}
    if "technical_deck_details" not in chat_cols:
        op.add_column(
            "chat_messages",
            sa.Column(
                "technical_deck_details",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )
    if "rfp_details" not in chat_cols:
        op.add_column(
            "chat_messages",
            sa.Column(
                "rfp_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True
            ),
        )
    op.alter_column(
        "chat_messages",
        "message_type",
        existing_type=sa.VARCHAR(length=50),
        nullable=True,
    )
    op.alter_column(
        "chat_messages", "role", existing_type=sa.VARCHAR(length=50), nullable=True
    )
    # Drop agent_data if present
    if "agent_data" in chat_cols:
        op.drop_column("chat_messages", "agent_data")
    op.alter_column(
        "chat_message_jobs",
        "status",
        existing_type=sa.VARCHAR(length=50),
        nullable=True,
    )
    op.alter_column(
        "attachments", "status", existing_type=sa.VARCHAR(length=50), nullable=True
    )
