"""add normalized chat job idempotency and WhatsApp delivery indexes

Revision ID: 3c7f79637723
Revises: 3c7f79637724
Create Date: 2026-07-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7f79637723"
down_revision: str | None = "3c7f79637724"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Normalize retry identity while preserving existing WhatsApp recovery state."""
    op.add_column(
        "chat_message_jobs",
        sa.Column("idempotency_key", sa.Text(), nullable=True),
    )

    # The channel is part of the identity namespace, so two providers may use
    # the same opaque key without colliding.
    op.execute(
        """
        create unique index uq_chat_message_jobs_channel_idempotency
        on chat_message_jobs (chat_type, idempotency_key)
        where idempotency_key is not null
        """
    )

    # Only WhatsApp currently runs the stale-job recovery query. Generalize this
    # index when the other bot channels adopt the same durable recovery sweep.
    op.execute(
        """
        create index ix_chat_message_jobs_whatsapp_recovery
        on chat_message_jobs (status, updated_at)
        where chat_type = 'WHATSAPP_CHAT'
        """
    )

    # WhatsApp media ingestion catches this conflict, removes the losing blob,
    # and reuses the row committed by the concurrent request.
    op.execute(
        """
        create unique index uq_attachments_whatsapp_source_external_id
        on attachments (source_external_id)
        where source_provider = 'whatsapp'
          and source_external_id is not null
        """
    )


def downgrade() -> None:
    """Restore the pre-normalization schema without deleting job metadata."""
    op.execute("drop index if exists uq_attachments_whatsapp_source_external_id")
    op.execute("drop index if exists ix_chat_message_jobs_whatsapp_recovery")
    op.execute("drop index if exists uq_chat_message_jobs_channel_idempotency")
    op.drop_column("chat_message_jobs", "idempotency_key")
