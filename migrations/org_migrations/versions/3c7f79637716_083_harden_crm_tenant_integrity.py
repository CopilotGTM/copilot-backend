"""Prevent CRM relationships and business keys from crossing tenants.

Revision ID: 3c7f79637716
Revises: 3c7f79637717
Create Date: 2026-07-30

The legacy per-organisation schemas already make cross-tenant rows unlikely,
but the future shared schema needs the database to prove the boundary. New
indexes are built before constraints, composite foreign keys are validated
before legacy keys are removed, and scoped unique indexes replace the four CRM
rules that were accidentally global in a shared schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from migrations.org_migrations.tenant_identity import validate_migration_schema_name

revision: str = "3c7f79637716"
down_revision: str | None = "3c7f79637717"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (parent table, parent identity column)
CRM_CANDIDATE_KEYS = (
    ("crm_companies", "company_id"),
    ("crm_contacts", "contact_id"),
    ("crm_deal_stages", "stage_id"),
    ("crm_deals", "deal_id"),
    ("crm_leads", "lead_id"),
    ("crm_pipelines", "pipeline_id"),
    ("crm_users", "user_id"),
    ("deal_jobs", "job_id"),
    ("knowledge_integrations", "integration_id"),
    ("lead_forms", "form_id"),
)

# (child table, local identity, parent table, parent identity, ON DELETE)
CRM_TENANT_FOREIGN_KEYS = (
    ("crm_activities", "company_id", "crm_companies", "company_id", None),
    ("crm_activities", "contact_id", "crm_contacts", "contact_id", None),
    ("crm_activities", "deal_id", "crm_deals", "deal_id", None),
    (
        "crm_activities",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "crm_activities",
        "source_integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "crm_companies",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("crm_companies", "owner_id", "crm_users", "user_id", None),
    ("crm_contacts", "company_id", "crm_companies", "company_id", None),
    (
        "crm_contacts",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("crm_contacts", "owner_id", "crm_users", "user_id", None),
    (
        "crm_custom_field_definitions",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("crm_deal_participants", "deal_id", "crm_deals", "deal_id", None),
    (
        "crm_deal_participants",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "crm_deal_stage_history",
        "deal_id",
        "crm_deals",
        "deal_id",
        None,
    ),
    (
        "crm_deal_stage_history",
        "from_stage_id",
        "crm_deal_stages",
        "stage_id",
        None,
    ),
    (
        "crm_deal_stage_history",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "crm_deal_stage_history",
        "stage_id",
        "crm_deal_stages",
        "stage_id",
        None,
    ),
    (
        "crm_deal_stages",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "crm_deal_stages",
        "pipeline_id",
        "crm_pipelines",
        "pipeline_id",
        None,
    ),
    ("crm_deals", "company_id", "crm_companies", "company_id", None),
    ("crm_deals", "created_by_user_id", "crm_users", "user_id", None),
    (
        "crm_deals",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("crm_deals", "owner_id", "crm_users", "user_id", None),
    ("crm_deals", "pipeline_id", "crm_pipelines", "pipeline_id", None),
    (
        "crm_deals",
        "primary_contact_id",
        "crm_contacts",
        "contact_id",
        None,
    ),
    ("crm_deals", "stage_id", "crm_deal_stages", "stage_id", None),
    ("crm_deals", "updated_by_user_id", "crm_users", "user_id", None),
    ("crm_import_rows", "import_job_id", "deal_jobs", "job_id", "CASCADE"),
    (
        "crm_lead_participants",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("crm_lead_participants", "lead_id", "crm_leads", "lead_id", None),
    ("crm_lead_status_history", "lead_id", "crm_leads", "lead_id", None),
    ("crm_leads", "company_id", "crm_companies", "company_id", None),
    ("crm_leads", "contact_id", "crm_contacts", "contact_id", None),
    ("crm_leads", "converted_deal_id", "crm_deals", "deal_id", None),
    ("crm_leads", "created_by_user_id", "crm_users", "user_id", None),
    (
        "crm_leads",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("crm_leads", "owner_id", "crm_users", "user_id", None),
    ("crm_leads", "updated_by_user_id", "crm_users", "user_id", None),
    ("crm_mutation_events", "deal_id", "crm_deals", "deal_id", None),
    ("crm_notes", "author_user_id", "crm_users", "user_id", None),
    ("crm_notes", "deal_id", "crm_deals", "deal_id", None),
    (
        "crm_notes",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "crm_object_associations",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "crm_pipelines",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "crm_source_object_mappings",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        "CASCADE",
    ),
    ("crm_tasks", "assigned_to_user_id", "crm_users", "user_id", None),
    ("crm_tasks", "completed_by_user_id", "crm_users", "user_id", None),
    ("crm_tasks", "deal_id", "crm_deals", "deal_id", None),
    (
        "crm_tasks",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("crm_tasks", "owner_user_id", "crm_users", "user_id", None),
    (
        "crm_users",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("lead_form_fields", "form_id", "lead_forms", "form_id", None),
    (
        "lead_form_fields",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("lead_form_submissions", "form_id", "lead_forms", "form_id", None),
    (
        "lead_form_submissions",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("lead_form_submissions", "lead_id", "crm_leads", "lead_id", None),
    ("lead_forms", "created_by_user_id", "crm_users", "user_id", None),
    (
        "lead_forms",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("lead_forms", "updated_by_user_id", "crm_users", "user_id", None),
)

# Only relationships without an existing organisation-leading access path.
CRM_FOREIGN_KEY_INDEXES = (
    ("crm_activities", "company_id"),
    ("crm_activities", "contact_id"),
    ("crm_activities", "deal_id"),
    ("crm_activities", "integration_id"),
    ("crm_activities", "source_integration_id"),
    ("crm_companies", "integration_id"),
    ("crm_companies", "owner_id"),
    ("crm_contacts", "integration_id"),
    ("crm_contacts", "owner_id"),
    ("crm_custom_field_definitions", "integration_id"),
    ("crm_deal_participants", "integration_id"),
    ("crm_deal_stage_history", "deal_id"),
    ("crm_deal_stage_history", "from_stage_id"),
    ("crm_deal_stage_history", "integration_id"),
    ("crm_deal_stage_history", "stage_id"),
    ("crm_deal_stages", "pipeline_id"),
    ("crm_deals", "integration_id"),
    ("crm_deals", "owner_id"),
    ("crm_lead_participants", "integration_id"),
    ("crm_leads", "company_id"),
    ("crm_leads", "contact_id"),
    ("crm_leads", "created_by_user_id"),
    ("crm_leads", "integration_id"),
    ("crm_leads", "updated_by_user_id"),
    ("crm_notes", "integration_id"),
    ("crm_pipelines", "integration_id"),
    ("crm_tasks", "completed_by_user_id"),
    ("crm_tasks", "integration_id"),
    ("crm_tasks", "owner_user_id"),
    ("crm_users", "integration_id"),
    ("lead_form_fields", "form_id"),
    ("lead_form_fields", "integration_id"),
    ("lead_form_submissions", "integration_id"),
    ("lead_form_submissions", "lead_id"),
    ("lead_forms", "created_by_user_id"),
    ("lead_forms", "integration_id"),
    ("lead_forms", "updated_by_user_id"),
)

# (table, legacy name, scoped name, scoped columns, partial predicate)
CRM_SCOPED_UNIQUE_INDEXES = (
    (
        "crm_custom_field_definitions",
        "uq_crm_custom_field_provider_key_state",
        "uq_crm_custom_field_provider_key_state_org",
        (
            "organisation_id",
            "integration_id",
            "entity_type",
            "provider_field_key",
            "is_deleted",
        ),
        "field_source = 'provider'",
    ),
    (
        "crm_lead_participants",
        "uq_crm_lead_participant_local_role",
        "uq_crm_lead_participant_local_role_org",
        (
            "organisation_id",
            "lead_id",
            "participant_type",
            "related_local_id",
            "relationship_role",
        ),
        "related_local_id IS NOT NULL AND is_deleted = false",
    ),
    (
        "crm_lead_participants",
        "uq_crm_lead_participant_primary_role",
        "uq_crm_lead_participant_primary_role_org",
        (
            "organisation_id",
            "lead_id",
            "participant_type",
            "relationship_role",
        ),
        "is_primary = true AND is_deleted = false",
    ),
    (
        "lead_form_fields",
        "uq_lead_form_field_key_active",
        "uq_lead_form_field_key_active_org",
        ("organisation_id", "form_id", "external_key", "is_deleted"),
        None,
    ),
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _qualified(schema_name: str, object_name: str) -> str:
    return f"{_quote_identifier(schema_name)}.{_quote_identifier(object_name)}"


def _migration_schema() -> str:
    """Return the validated shared-schema target."""

    x_args = context.get_x_argument(as_dictionary=True)
    schema_name = x_args.get("schema")
    if schema_name is None:
        raise ValueError("schema is required for organisation migrations")
    return validate_migration_schema_name(schema_name)


def _constraint_exists(schema_name: str, table_name: str, name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT 1
                  FROM pg_constraint constraint_record
                  JOIN pg_class table_record
                    ON table_record.oid = constraint_record.conrelid
                  JOIN pg_namespace namespace_record
                    ON namespace_record.oid = table_record.relnamespace
                 WHERE namespace_record.nspname = :schema_name
                   AND table_record.relname = :table_name
                   AND constraint_record.conname = :constraint_name
                """
            ),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "constraint_name": name,
            },
        )
        .scalar_one_or_none()
    )


def _index_validity(schema_name: str, index_name: str) -> bool | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT index_record.indisvalid
                  FROM pg_index index_record
                  JOIN pg_class class_record
                    ON class_record.oid = index_record.indexrelid
                  JOIN pg_namespace namespace_record
                    ON namespace_record.oid = class_record.relnamespace
                 WHERE namespace_record.nspname = :schema_name
                   AND class_record.relname = :index_name
                """
            ),
            {"schema_name": schema_name, "index_name": index_name},
        )
        .scalar_one_or_none()
    )


def _ensure_concurrent_index(
    schema_name: str,
    table_name: str,
    index_name: str,
    columns: Sequence[str],
    *,
    unique: bool = False,
    where: str | None = None,
) -> None:
    """Build one resumable index without blocking CRM writes for the full scan."""

    validity = _index_validity(schema_name, index_name)
    if validity is True:
        return

    # A failed concurrent build leaves an invalid index behind. Remove only
    # that deterministic Task 4 object before retrying the same definition.
    with op.get_context().autocommit_block():
        if validity is False:
            op.drop_index(
                index_name,
                schema=schema_name,
                postgresql_concurrently=True,
            )
        op.create_index(
            index_name,
            table_name,
            list(columns),
            schema=schema_name,
            unique=unique,
            postgresql_concurrently=True,
            postgresql_where=sa.text(where) if where else None,
        )


def _drop_index_concurrently(schema_name: str, index_name: str) -> None:
    if _index_validity(schema_name, index_name) is None:
        return
    with op.get_context().autocommit_block():
        op.drop_index(
            index_name,
            schema=schema_name,
            postgresql_concurrently=True,
        )


def _candidate_name(table_name: str, identity_column: str) -> str:
    return f"uq_{table_name}_org_{identity_column}"


def _foreign_key_name(table_name: str, local_column: str) -> str:
    return f"fk_{table_name}_{local_column}_org"


def _foreign_key_index_name(table_name: str, local_column: str) -> str:
    return f"ix_{table_name}_org_{local_column}"


def _legacy_foreign_key_name(
    schema_name: str,
    child_table: str,
    local_column: str,
    parent_table: str,
    remote_column: str,
) -> str | None:
    """Discover the actual legacy name instead of assuming Alembic naming."""

    return (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT constraint_record.conname
                  FROM pg_constraint constraint_record
                  JOIN pg_class child_record
                    ON child_record.oid = constraint_record.conrelid
                  JOIN pg_namespace child_namespace
                    ON child_namespace.oid = child_record.relnamespace
                  JOIN pg_class parent_record
                    ON parent_record.oid = constraint_record.confrelid
                  JOIN pg_namespace parent_namespace
                    ON parent_namespace.oid = parent_record.relnamespace
                  JOIN pg_attribute child_column
                    ON child_column.attrelid = child_record.oid
                   AND child_column.attnum = constraint_record.conkey[1]
                  JOIN pg_attribute parent_column
                    ON parent_column.attrelid = parent_record.oid
                   AND parent_column.attnum = constraint_record.confkey[1]
                 WHERE constraint_record.contype = 'f'
                   AND cardinality(constraint_record.conkey) = 1
                   AND child_namespace.nspname = :schema_name
                   AND parent_namespace.nspname = :schema_name
                   AND child_record.relname = :child_table
                   AND child_column.attname = :local_column
                   AND parent_record.relname = :parent_table
                   AND parent_column.attname = :remote_column
                """
            ),
            {
                "schema_name": schema_name,
                "child_table": child_table,
                "local_column": local_column,
                "parent_table": parent_table,
                "remote_column": remote_column,
            },
        )
        .scalar_one_or_none()
    )


def _drop_legacy_foreign_key(
    schema_name: str,
    child_table: str,
    local_column: str,
    parent_table: str,
    remote_column: str,
) -> str | None:
    """Remove the discovered single-column FK after its replacement is valid."""

    legacy_name = _legacy_foreign_key_name(
        schema_name,
        child_table,
        local_column,
        parent_table,
        remote_column,
    )
    if legacy_name is not None:
        op.drop_constraint(
            legacy_name,
            child_table,
            schema=schema_name,
            type_="foreignkey",
        )
    return legacy_name


def _validate_constraint(
    schema_name: str,
    table_name: str,
    constraint_name: str,
) -> None:
    # All identifiers come from the validated schema and fixed inventories.
    op.execute(
        f"ALTER TABLE {_qualified(schema_name, table_name)} "  # noqa: S608
        f"VALIDATE CONSTRAINT {_quote_identifier(constraint_name)}"
    )


def upgrade() -> None:
    """Install CRM tenant keys while preserving protection during the swap."""

    schema_name = _migration_schema()
    if _constraint_exists(
        schema_name,
        "crm_deal_participants",
        "uq_crm_deal_participant_unique",
    ):
        op.drop_constraint(
            "uq_crm_deal_participant_unique",
            "crm_deal_participants",
            schema=schema_name,
            type_="unique",
        )

    # Candidate indexes are built concurrently, then attached without another
    # data scan so large CRM parents remain writable during the expensive step.
    for table_name, identity_column in CRM_CANDIDATE_KEYS:
        constraint_name = _candidate_name(table_name, identity_column)
        if _constraint_exists(schema_name, table_name, constraint_name):
            continue
        _ensure_concurrent_index(
            schema_name,
            table_name,
            constraint_name,
            ("organisation_id", identity_column),
            unique=True,
        )
        op.execute(
            f"ALTER TABLE {_qualified(schema_name, table_name)} "  # noqa: S608
            f"ADD CONSTRAINT {_quote_identifier(constraint_name)} "
            f"UNIQUE USING INDEX {_quote_identifier(constraint_name)}"
        )

    for table_name, local_column in CRM_FOREIGN_KEY_INDEXES:
        _ensure_concurrent_index(
            schema_name,
            table_name,
            _foreign_key_index_name(table_name, local_column),
            ("organisation_id", local_column),
        )

    for (
        child_table,
        local_column,
        parent_table,
        remote_column,
        ondelete,
    ) in CRM_TENANT_FOREIGN_KEYS:
        constraint_name = _foreign_key_name(child_table, local_column)
        if not _constraint_exists(schema_name, child_table, constraint_name):
            op.create_foreign_key(
                constraint_name,
                child_table,
                parent_table,
                ["organisation_id", local_column],
                ["organisation_id", remote_column],
                source_schema=schema_name,
                referent_schema=schema_name,
                ondelete=ondelete,
                postgresql_not_valid=True,
            )
            _validate_constraint(schema_name, child_table, constraint_name)

        _drop_legacy_foreign_key(
            schema_name,
            child_table,
            local_column,
            parent_table,
            remote_column,
        )

    # The scoped rule is live before the legacy global rule is removed, so a
    # concurrent write always has at least one uniqueness guard.
    for (
        table_name,
        legacy_name,
        scoped_name,
        columns,
        predicate,
    ) in CRM_SCOPED_UNIQUE_INDEXES:
        _ensure_concurrent_index(
            schema_name,
            table_name,
            scoped_name,
            columns,
            unique=True,
            where=predicate,
        )
        _drop_index_concurrently(schema_name, legacy_name)


def downgrade() -> None:
    """Restore legacy rules before removing their tenant-qualified replacements."""

    schema_name = _migration_schema()
    if not _constraint_exists(
        schema_name,
        "crm_deal_participants",
        "uq_crm_deal_participant_unique",
    ):
        op.create_unique_constraint(
            "uq_crm_deal_participant_unique",
            "crm_deal_participants",
            [
                "deal_id",
                "participant_type",
                "related_local_id",
                "relationship_role",
            ],
            schema=schema_name,
        )

    for (
        table_name,
        legacy_name,
        scoped_name,
        columns,
        predicate,
    ) in reversed(CRM_SCOPED_UNIQUE_INDEXES):
        _ensure_concurrent_index(
            schema_name,
            table_name,
            legacy_name,
            tuple(column for column in columns if column != "organisation_id"),
            unique=True,
            where=predicate,
        )
        _drop_index_concurrently(schema_name, scoped_name)

    for (
        child_table,
        local_column,
        parent_table,
        remote_column,
        ondelete,
    ) in reversed(CRM_TENANT_FOREIGN_KEYS):
        legacy_name = f"{child_table}_{local_column}_fkey"
        if not _constraint_exists(schema_name, child_table, legacy_name):
            op.create_foreign_key(
                legacy_name,
                child_table,
                parent_table,
                [local_column],
                [remote_column],
                source_schema=schema_name,
                referent_schema=schema_name,
                ondelete=ondelete,
                postgresql_not_valid=True,
            )
            _validate_constraint(schema_name, child_table, legacy_name)
        op.drop_constraint(
            _foreign_key_name(child_table, local_column),
            child_table,
            schema=schema_name,
            type_="foreignkey",
        )

    for table_name, local_column in reversed(CRM_FOREIGN_KEY_INDEXES):
        _drop_index_concurrently(
            schema_name,
            _foreign_key_index_name(table_name, local_column),
        )

    for table_name, identity_column in reversed(CRM_CANDIDATE_KEYS):
        op.drop_constraint(
            _candidate_name(table_name, identity_column),
            table_name,
            schema=schema_name,
            type_="unique",
        )
