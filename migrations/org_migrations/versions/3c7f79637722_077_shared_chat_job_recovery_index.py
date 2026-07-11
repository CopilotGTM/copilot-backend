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
    with op.get_context().autocommit_block():
        # Build the replacement without blocking chat-job writes on large tenants.
        op.execute(
            """
            create index concurrently if not exists ix_chat_message_jobs_recovery
            on chat_message_jobs (chat_type, status, updated_at)
            where status in ('pending', 'in_progress', 'failed')
            """
        )
        # Remove the narrower index only after the shared replacement is usable.
        op.execute("drop index concurrently if exists ix_chat_message_jobs_whatsapp_recovery")


def downgrade() -> None:
    """Restore the WhatsApp-only recovery index."""
    with op.get_context().autocommit_block():
        # Restore the old access path before removing the shared replacement.
        op.execute(
            """
            create index concurrently if not exists ix_chat_message_jobs_whatsapp_recovery
            on chat_message_jobs (status, updated_at)
            where chat_type = 'WHATSAPP_CHAT'
            """
        )
        op.execute("drop index concurrently if exists ix_chat_message_jobs_recovery")
