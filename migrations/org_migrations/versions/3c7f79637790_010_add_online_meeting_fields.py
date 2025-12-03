"""010 add online meeting fields

Revision ID: 3c7f79637790
Revises: 3c7f79637791
Create Date: 2025-11-29 12:00:00.000000

Adds online_meeting_id and online_meeting_provider fields to calendar_events table
for Teams/Zoom/Google Meet transcript integration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision: str = '3c7f79637790'
down_revision: Union[str, None] = '3c7f79637791'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add online meeting fields for transcript support."""
    # Add online_meeting_id column (used for transcript API access)
    op.add_column(
        'calendar_events',
        sa.Column('online_meeting_id', sa.String(length=512), nullable=True)
    )
    
    # Add online_meeting_provider column (teams, googleMeet, zoom, etc.)
    op.add_column(
        'calendar_events',
        sa.Column('online_meeting_provider', sa.String(length=50), nullable=True)
    )
    
    # Create index on online_meeting_id for transcript lookups
    op.create_index(
        'ix_calendar_events_online_meeting_id',
        'calendar_events',
        ['online_meeting_id'],
        unique=False
    )


def downgrade() -> None:
    """Remove online meeting fields."""
    op.drop_index('ix_calendar_events_online_meeting_id', table_name='calendar_events')
    op.drop_column('calendar_events', 'online_meeting_provider')
    op.drop_column('calendar_events', 'online_meeting_id')
