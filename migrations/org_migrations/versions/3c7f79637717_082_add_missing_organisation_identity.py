"""Add direct organisation identity to the nine inferred-tenant tables.

Revision ID: 3c7f79637717
Revises: 3c7f79637718
Create Date: 2026-07-30

The shared schema must be empty when this revision runs. A selected legacy
``org_<uuid>`` schema is backfilled from its canonical schema identity before
the selective cutover copies its rows into the shared schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from migrations.org_migrations.tenant_identity import (
    legacy_schema_organisation_id,
    validate_migration_schema_name,
)
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637717"
down_revision: str | None = "3c7f79637718"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_ORGANISATION_ID_DEFAULT_SQL = (
    "NULLIF(current_setting('app.current_organisation_id', true), '')::uuid"
)
TENANT_IDENTITY_TABLES = (
    "calendar_event_attendees",
    "calendar_event_user_states",
    "chat_message_attachment_link",
    "chat_message_jobs",
    "chat_messages",
    "integration_sync_history",
    "knowledge_base",
    "knowledge_processing_jobs",
    "transcript_segments",
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _qualified(schema_name: str, table_name: str) -> str:
    return f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"


def _migration_schema() -> str:
    """Return the validated empty shared-schema target."""

    x_args = context.get_x_argument(as_dictionary=True)
    schema_name = x_args.get("schema")
    if schema_name is None:
        raise ValueError("schema is required for organisation migrations")
    return validate_migration_schema_name(schema_name)


def upgrade() -> None:
    """Add row identity, backfilling only a canonical legacy organisation schema."""

    schema_name = _migration_schema()
    organisation_id = (
        legacy_schema_organisation_id(schema_name)
        if schema_name.startswith("org_")
        else None
    )

    for table_name in TENANT_IDENTITY_TABLES:
        qualified_table = _qualified(schema_name, table_name)
        check_name = f"ck_{table_name}_organisation_id_nn"
        foreign_key_name = f"fk_{table_name}_organisation_id_organisations"

        op.add_column(
            table_name,
            sa.Column(
                "organisation_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            schema=schema_name,
        )

        if organisation_id is not None:
            # The canonical legacy schema name is the ownership boundary, so a
            # caller cannot assign an unrelated UUID through an Alembic argument.
            op.get_bind().execute(
                sa.text(
                    f"UPDATE {qualified_table} "  # noqa: S608
                    "SET organisation_id = :organisation_id "
                    "WHERE organisation_id IS NULL"
                ),
                {"organisation_id": str(organisation_id)},
            )

        # Validation deliberately rejects populated tenant_data or test schemas;
        # only canonical legacy schemas are eligible for the one-time backfill.
        op.create_check_constraint(
            check_name,
            table_name,
            "organisation_id IS NOT NULL",
            schema=schema_name,
            postgresql_not_valid=True,
        )
        op.execute(
            f"ALTER TABLE {qualified_table} VALIDATE CONSTRAINT {_quote_identifier(check_name)}"
        )
        op.alter_column(
            table_name,
            "organisation_id",
            schema=schema_name,
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=False,
        )
        op.alter_column(
            table_name,
            "organisation_id",
            schema=schema_name,
            existing_type=postgresql.UUID(as_uuid=True),
            server_default=sa.text(TENANT_ORGANISATION_ID_DEFAULT_SQL),
        )
        op.create_index(
            f"ix_{table_name}_organisation_id",
            table_name,
            ["organisation_id"],
            schema=schema_name,
        )
        op.create_foreign_key(
            foreign_key_name,
            table_name,
            "organisations",
            ["organisation_id"],
            ["organisation_id"],
            source_schema=schema_name,
            referent_schema="public",
            ondelete="RESTRICT",
            postgresql_not_valid=True,
        )
        # Validate separately so the ownership check does not hold the stronger
        # constraint-creation lock while PostgreSQL scans existing rows.
        op.execute(
            f"ALTER TABLE {qualified_table} "
            f"VALIDATE CONSTRAINT {_quote_identifier(foreign_key_name)}"
        )
        op.drop_constraint(
            check_name,
            table_name,
            schema=schema_name,
            type_="check",
        )


def downgrade() -> None:
    """Remove the additive identity columns in reverse dependency order."""

    schema_name = _migration_schema()
    for table_name in reversed(TENANT_IDENTITY_TABLES):
        op.drop_constraint(
            f"fk_{table_name}_organisation_id_organisations",
            table_name,
            schema=schema_name,
            type_="foreignkey",
        )
        op.drop_index(
            f"ix_{table_name}_organisation_id",
            table_name=table_name,
            schema=schema_name,
        )
        op.drop_column(
            table_name,
            "organisation_id",
            schema=schema_name,
        )
