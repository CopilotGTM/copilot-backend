"""009 deal jobs

Revision ID: 3c7f79637791
Revises: 3c7f79637792
Create Date: 2025-11-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3c7f79637791'
down_revision: Union[str, None] = '3c7f79637792'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create deal_jobs table for async deal operations."""
    op.create_table(
        'deal_jobs',
        sa.Column('job_id', sa.Uuid(), nullable=False),
        sa.Column('organisation_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('job_type', sa.String(length=50), nullable=False),
        sa.Column('deal_id', sa.Uuid(), nullable=False),
        sa.Column('event_id', sa.Uuid(), nullable=True),
        sa.Column('stakeholder_id', sa.Uuid(), nullable=True),
        sa.Column('status', sa.String(length=50), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('job_metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=True),
        sa.Column('result_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['deal_id'], ['crm_deals.deal_id'], ),
        sa.ForeignKeyConstraint(['event_id'], ['calendar_events.event_id'], ),
        sa.ForeignKeyConstraint(['stakeholder_id'], ['deal_stakeholders.stakeholder_id'], ),
        sa.PrimaryKeyConstraint('job_id'),
    )
    
    # Indexes for common query patterns
    op.create_index('ix_deal_jobs_organisation_id', 'deal_jobs', ['organisation_id'], unique=False)
    op.create_index('ix_deal_jobs_user_id', 'deal_jobs', ['user_id'], unique=False)
    op.create_index('ix_deal_jobs_job_type', 'deal_jobs', ['job_type'], unique=False)
    op.create_index('ix_deal_jobs_deal_id', 'deal_jobs', ['deal_id'], unique=False)
    op.create_index('ix_deal_jobs_event_id', 'deal_jobs', ['event_id'], unique=False)
    op.create_index('ix_deal_jobs_stakeholder_id', 'deal_jobs', ['stakeholder_id'], unique=False)
    op.create_index('ix_deal_jobs_org_deal', 'deal_jobs', ['organisation_id', 'deal_id'], unique=False)
    op.create_index('ix_deal_jobs_org_status', 'deal_jobs', ['organisation_id', 'status'], unique=False)
    op.create_index('ix_deal_jobs_type_status', 'deal_jobs', ['job_type', 'status'], unique=False)
    # Composite index for active job lookup (job recovery on reconnect)
    op.create_index(
        'ix_deal_jobs_active_lookup', 
        'deal_jobs', 
        ['organisation_id', 'deal_id', 'job_type', 'event_id', 'status'], 
        unique=False
    )


def downgrade() -> None:
    """Drop deal_jobs table."""
    op.drop_index('ix_deal_jobs_active_lookup', table_name='deal_jobs')
    op.drop_index('ix_deal_jobs_type_status', table_name='deal_jobs')
    op.drop_index('ix_deal_jobs_org_status', table_name='deal_jobs')
    op.drop_index('ix_deal_jobs_org_deal', table_name='deal_jobs')
    op.drop_index('ix_deal_jobs_stakeholder_id', table_name='deal_jobs')
    op.drop_index('ix_deal_jobs_event_id', table_name='deal_jobs')
    op.drop_index('ix_deal_jobs_deal_id', table_name='deal_jobs')
    op.drop_index('ix_deal_jobs_job_type', table_name='deal_jobs')
    op.drop_index('ix_deal_jobs_user_id', table_name='deal_jobs')
    op.drop_index('ix_deal_jobs_organisation_id', table_name='deal_jobs')
    op.drop_table('deal_jobs')
