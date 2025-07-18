"""Add Supabase support and chat message timestamps

Revision ID: 001
Revises: 
Create Date: 2024-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add supabase_user_id to users table
    op.add_column('users', sa.Column('supabase_user_id', sa.String(), nullable=True))
    op.create_index(op.f('ix_users_supabase_user_id'), 'users', ['supabase_user_id'], unique=True)
    
    # Add created_at and updated_at to chat_messages table
    op.add_column('chat_messages', sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')))
    op.add_column('chat_messages', sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')))
    
    # Comment out the timestamp column (since it's commented in the model)
    # Note: This is a no-op since the column already exists, but we're documenting the change
    # op.alter_column('chat_messages', 'timestamp', nullable=True, comment='Commented out - using created_at instead')
    
    # Drop user_credentials table since we're using Supabase
    op.drop_table('user_credentials')


def downgrade() -> None:
    # Recreate user_credentials table
    op.create_table('user_credentials',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('organisation_id', sa.UUID(), nullable=False),
        sa.Column('hashed_password', sa.String(), nullable=False),
        sa.Column('last_password_change', sa.DateTime(), nullable=False),
        sa.Column('password_reset_token', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.organisation_id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ),
        sa.PrimaryKeyConstraint('user_id', 'email', 'organisation_id')
    )
    
    # Remove created_at and updated_at from chat_messages
    op.drop_column('chat_messages', 'updated_at')
    op.drop_column('chat_messages', 'created_at')
    
    # Remove supabase_user_id from users table
    op.drop_index(op.f('ix_users_supabase_user_id'), table_name='users')
    op.drop_column('users', 'supabase_user_id') 