"""Shared SQLAlchemy declarations for organisation-owned relationships.

Organisation models use this declaration when both sides of a relationship
live in tenant data. Including the organisation key in the foreign key prevents
a valid row identifier from linking records owned by different tenants.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint


def tenant_foreign_key(
    table_name: str,
    local_column: str,
    target_table: str,
    target_column: str,
    *,
    ondelete: str | None = None,
) -> ForeignKeyConstraint:
    """Declare one relationship whose child and parent tenant must match."""

    return ForeignKeyConstraint(
        ["organisation_id", local_column],
        [
            f"{target_table}.organisation_id",
            f"{target_table}.{target_column}",
        ],
        name=f"fk_{table_name}_{local_column}_org",
        ondelete=ondelete,
    )
