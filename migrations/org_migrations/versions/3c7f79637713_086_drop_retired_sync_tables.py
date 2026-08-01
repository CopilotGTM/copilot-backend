"""Remove retired sync tables after their runtime code has been deleted.

Native transcript data remains in ``meetings`` and ``transcript_segments``;
deal feed membership remains in ``crm_activities`` and ``crm_activity_links``.
The removed rows cannot be reconstructed during downgrade, so rollback requires
restoring these tables from a database backup.

Revision ID: 3c7f79637713
Revises: 3c7f79637714
Create Date: 2026-08-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "3c7f79637713"
down_revision: str | None = "3c7f79637714"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# CI transcript segments depend on calls and speakers. Keep the CI children
# before ``ci_calls`` so the migration does not rely on cascading deletion.
TABLES_IN_DROP_ORDER = (
    "ci_call_analytics",
    "ci_transcript_segments",
    "ci_call_speakers",
    "ci_call_deal_links",
    "ci_calls",
    "integration_participant_identities",
    "messaging_thread_summaries",
    "email_thread_summaries",
    "deal_timeline_events",
)


def upgrade() -> None:
    """Drop every retired sync table in dependency-safe order."""
    for table_name in TABLES_IN_DROP_ORDER:
        op.drop_table(table_name, if_exists=True)


def downgrade() -> None:
    """Reject rollback because restoring empty tables would not restore their data."""
    # copilotgtm: Restore the retired tables and rows from backup before rollback.
    raise RuntimeError(
        "3c7f79637713 is irreversible; restore the retired sync tables from backup "
        "before rollback"
    )
