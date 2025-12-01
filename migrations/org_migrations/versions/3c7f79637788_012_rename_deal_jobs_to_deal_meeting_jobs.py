"""012 rename deal_jobs to deal_meeting_jobs

Revision ID: 3c7f79637788
Revises: 3c7f79637789
Create Date: 2025-11-30 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3c7f79637788'
down_revision: Union[str, None] = '3c7f79637789'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename deal_jobs table to deal_meeting_jobs."""
    # Rename the table
    op.rename_table('deal_jobs', 'deal_meeting_jobs')
    
    # Rename indexes to match new table name
    # Note: PostgreSQL automatically updates foreign key references when table is renamed
    op.execute('ALTER INDEX ix_deal_jobs_organisation_id RENAME TO ix_deal_meeting_jobs_organisation_id')
    op.execute('ALTER INDEX ix_deal_jobs_user_id RENAME TO ix_deal_meeting_jobs_user_id')
    op.execute('ALTER INDEX ix_deal_jobs_job_type RENAME TO ix_deal_meeting_jobs_job_type')
    op.execute('ALTER INDEX ix_deal_jobs_deal_id RENAME TO ix_deal_meeting_jobs_deal_id')
    op.execute('ALTER INDEX ix_deal_jobs_event_id RENAME TO ix_deal_meeting_jobs_event_id')
    op.execute('ALTER INDEX ix_deal_jobs_stakeholder_id RENAME TO ix_deal_meeting_jobs_stakeholder_id')
    op.execute('ALTER INDEX ix_deal_jobs_org_deal RENAME TO ix_deal_meeting_jobs_org_deal')
    op.execute('ALTER INDEX ix_deal_jobs_org_status RENAME TO ix_deal_meeting_jobs_org_status')
    op.execute('ALTER INDEX ix_deal_jobs_type_status RENAME TO ix_deal_meeting_jobs_type_status')
    op.execute('ALTER INDEX ix_deal_jobs_active_lookup RENAME TO ix_deal_meeting_jobs_active_lookup')


def downgrade() -> None:
    """Revert: rename deal_meeting_jobs back to deal_jobs."""
    # Rename indexes back
    op.execute('ALTER INDEX ix_deal_meeting_jobs_active_lookup RENAME TO ix_deal_jobs_active_lookup')
    op.execute('ALTER INDEX ix_deal_meeting_jobs_type_status RENAME TO ix_deal_jobs_type_status')
    op.execute('ALTER INDEX ix_deal_meeting_jobs_org_status RENAME TO ix_deal_jobs_org_status')
    op.execute('ALTER INDEX ix_deal_meeting_jobs_org_deal RENAME TO ix_deal_jobs_org_deal')
    op.execute('ALTER INDEX ix_deal_meeting_jobs_stakeholder_id RENAME TO ix_deal_jobs_stakeholder_id')
    op.execute('ALTER INDEX ix_deal_meeting_jobs_event_id RENAME TO ix_deal_jobs_event_id')
    op.execute('ALTER INDEX ix_deal_meeting_jobs_deal_id RENAME TO ix_deal_jobs_deal_id')
    op.execute('ALTER INDEX ix_deal_meeting_jobs_job_type RENAME TO ix_deal_jobs_job_type')
    op.execute('ALTER INDEX ix_deal_meeting_jobs_user_id RENAME TO ix_deal_jobs_user_id')
    op.execute('ALTER INDEX ix_deal_meeting_jobs_organisation_id RENAME TO ix_deal_jobs_organisation_id')
    
    # Rename the table back
    op.rename_table('deal_meeting_jobs', 'deal_jobs')
