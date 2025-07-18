"""Add organization schema and role fields

Revision ID: 002
Revises: 001
Create Date: 2024-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add schema_name and role_name to organisations table
    op.add_column('organisations', sa.Column('schema_name', sa.String(), nullable=True))
    op.add_column('organisations', sa.Column('role_name', sa.String(), nullable=True))
    
    # Create indexes for schema and role names
    op.create_index(op.f('ix_organisations_schema_name'), 'organisations', ['schema_name'], unique=True)
    op.create_index(op.f('ix_organisations_role_name'), 'organisations', ['role_name'], unique=True)
    
    # Add migration tracking table for organizations
    op.create_table('org_migrations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organisation_id', sa.UUID(), nullable=False),
        sa.Column('schema_name', sa.String(), nullable=False),
        sa.Column('migration_version', sa.String(), nullable=False),
        sa.Column('applied_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_org_migrations_organisation_id'), 'org_migrations', ['organisation_id'], unique=False)
    op.create_index(op.f('ix_org_migrations_schema_name'), 'org_migrations', ['schema_name'], unique=False)


def downgrade() -> None:
    # Drop org_migrations table
    op.drop_index(op.f('ix_org_migrations_schema_name'), table_name='org_migrations')
    op.drop_index(op.f('ix_org_migrations_organisation_id'), table_name='org_migrations')
    op.drop_table('org_migrations')
    
    # Drop indexes
    op.drop_index(op.f('ix_organisations_role_name'), table_name='organisations')
    op.drop_index(op.f('ix_organisations_schema_name'), table_name='organisations')
    
    # Drop columns
    op.drop_column('organisations', 'role_name')
    op.drop_column('organisations', 'schema_name') 