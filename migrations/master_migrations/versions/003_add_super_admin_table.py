"""Add super admin table

Revision ID: 003
Revises: 002
Create Date: 2024-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum types
    op.execute("CREATE TYPE adminrole AS ENUM ('super_admin', 'admin')")
    op.execute("CREATE TYPE adminstatus AS ENUM ('active', 'inactive', 'pending_setup')")
    
    # Create super_admins table
    op.create_table('super_admins',
        sa.Column('admin_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('supabase_user_id', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('first_name', sa.String(length=100), nullable=False),
        sa.Column('last_name', sa.String(length=100), nullable=False),
        sa.Column('role', sa.Enum('super_admin', 'admin', name='adminrole'), nullable=False),
        sa.Column('status', sa.Enum('active', 'inactive', 'pending_setup', name='adminstatus'), nullable=False),
        sa.Column('last_login_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('email_verified', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('admin_id'),
        sa.UniqueConstraint('email'),
        sa.UniqueConstraint('supabase_user_id')
    )
    
    # Create indexes
    op.create_index(op.f('ix_super_admins_email'), 'super_admins', ['email'], unique=True)
    op.create_index(op.f('ix_super_admins_supabase_user_id'), 'super_admins', ['supabase_user_id'], unique=True)
    op.create_index(op.f('ix_super_admins_role'), 'super_admins', ['role'], unique=False)
    op.create_index(op.f('ix_super_admins_status'), 'super_admins', ['status'], unique=False)


def downgrade() -> None:
    # Drop indexes
    op.drop_index(op.f('ix_super_admins_status'), table_name='super_admins')
    op.drop_index(op.f('ix_super_admins_role'), table_name='super_admins')
    op.drop_index(op.f('ix_super_admins_supabase_user_id'), table_name='super_admins')
    op.drop_index(op.f('ix_super_admins_email'), table_name='super_admins')
    
    # Drop table
    op.drop_table('super_admins')
    
    # Drop enum types
    op.execute("DROP TYPE adminstatus")
    op.execute("DROP TYPE adminrole") 