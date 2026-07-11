"""replace the WhatsApp-only recovery index with a shared chat-job index

Revision ID: 3c7f79637722
Revises: 3c7f79637723
Create Date: 2026-07-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3c7f79637722"
down_revision: str | None = "3c7f79637723"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Index the recovery paths used by WhatsApp and web chat."""
    # copilotgtm: Keep this DDL inside env.py's organization transaction because
    # Alembic cannot enter autocommit_block() when connection.begin() owns it.
    # This may briefly lock chat_message_jobs; change the shared transaction model
    # before restoring concurrent index builds if tenant job tables grow large.
    op.execute(
        """
        create index if not exists ix_chat_message_jobs_recovery
        on chat_message_jobs (chat_type, status, updated_at)
        where status in ('pending', 'in_progress', 'failed')
        """
    )
    # Remove the narrower index only after the shared replacement is usable.
    op.execute("drop index if exists ix_chat_message_jobs_whatsapp_recovery")


def downgrade() -> None:
    """Restore the WhatsApp-only recovery index."""
    # Restore the old access path before removing the shared replacement.
    op.execute(
        """
        create index if not exists ix_chat_message_jobs_whatsapp_recovery
        on chat_message_jobs (status, updated_at)
        where chat_type = 'WHATSAPP_CHAT'
        """
    )
    op.execute("drop index if exists ix_chat_message_jobs_recovery")
