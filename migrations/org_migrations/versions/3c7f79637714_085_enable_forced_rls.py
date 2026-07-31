"""Enable and force tenant row-level security.

Revision ID: 3c7f79637714
Revises: 3c7f79637715
"""

from __future__ import annotations

from alembic import context, op
from migrations.org_migrations.tenant_identity import validate_migration_schema_name
from migrations.org_migrations.tenant_policy import (
    install_tenant_rls,
    remove_tenant_rls,
)

revision: str = "3c7f79637714"
down_revision: str | None = "3c7f79637715"
branch_labels: str | None = None
depends_on: str | None = None


def _migration_schema() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    schema_name = x_args.get("schema")
    if schema_name is None:
        raise ValueError("schema is required for organisation migrations")
    return validate_migration_schema_name(schema_name)


def upgrade() -> None:
    """Protect every tenant-owned table present at this revision."""

    install_tenant_rls(op.get_bind(), schema_name=_migration_schema())


def downgrade() -> None:
    """Remove the policies installed by this revision."""

    remove_tenant_rls(op.get_bind(), schema_name=_migration_schema())
