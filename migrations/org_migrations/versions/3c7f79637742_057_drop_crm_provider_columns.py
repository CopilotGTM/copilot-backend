"""Drop canonical CRM provider identity columns.

Revision ID: 3c7f79637742
Revises: 3c7f79637743
Create Date: 2026-06-06 18:00:00.000000
"""

from alembic import op

revision = "3c7f79637742"
down_revision = "3c7f79637743"
branch_labels = None
depends_on = None


def _drop_indexes(*names: str) -> None:
    for name in names:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def _drop_constraints(table: str, *names: str) -> None:
    for name in names:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")


def _drop_columns(table: str, *columns: str) -> None:
    for column in columns:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")


def upgrade() -> None:
    """Remove provider IDs/payloads from canonical CRM tables.

    Provider identity and payload now live in crm_source_object_mappings. The
    canonical CRM tables keep only local IDs, denormalized display fields, and
    sync lifecycle timestamps.
    """
    _drop_indexes(
        "ix_crm_company_owner",
        "ix_crm_contact_owner",
        "ix_crm_contact_company_external",
        "ix_crm_deal_owner",
        "ix_crm_deals_owner_external_id",
        "ix_crm_deal_pipeline_external",
        "ix_crm_deals_pipeline_external_id",
        "ix_crm_deal_stage_external",
        "ix_crm_deals_stage_external_id",
        "ix_crm_deal_participant_related",
        "ix_crm_activity_owner",
        "ix_crm_activity_related",
        "ix_crm_assoc_from_object",
        "ix_crm_assoc_to_object",
    )

    _drop_constraints("crm_companies", "uq_crm_company_external")
    _drop_constraints("crm_contacts", "uq_crm_contact_external")
    _drop_constraints("crm_users", "uq_crm_user_external")
    _drop_constraints("crm_pipelines", "uq_crm_pipeline_external")
    _drop_constraints("crm_deal_stages", "uq_crm_stage_external")
    _drop_constraints("crm_deals", "uq_crm_deal_external")
    _drop_constraints(
        "crm_deal_participants",
        "uq_crm_deal_participant_external",
        "uq_crm_deal_participant_unique",
    )
    _drop_constraints("crm_deal_stage_history", "uq_crm_deal_stage_history_external")
    _drop_constraints(
        "crm_activities",
        "uq_crm_deal_task_external",
        "uq_crm_activity_external",
        "uq_crm_activity_external_user",
    )
    _drop_constraints(
        "crm_object_associations",
        "uq_crm_object_association_external",
        "uq_crm_object_association_logical",
    )

    _drop_columns("crm_companies", "external_id", "owner_external_id", "data_payload")
    _drop_columns(
        "crm_contacts",
        "external_id",
        "company_external_id",
        "owner_external_id",
        "data_payload",
    )
    _drop_columns("crm_users", "external_id", "data_payload")
    _drop_columns("crm_pipelines", "external_id", "data_payload")
    _drop_columns("crm_deal_stages", "external_id", "data_payload")
    _drop_columns(
        "crm_deals",
        "external_id",
        "stage_external_id",
        "pipeline_external_id",
        "owner_external_id",
        "created_by_user_external_id",
        "updated_by_user_external_id",
        "company_external_id",
        "primary_contact_external_id",
        "associations_payload",
        "data_payload",
    )
    _drop_columns("crm_deal_participants", "external_id", "related_external_id", "data_payload")
    _drop_columns(
        "crm_deal_stage_history",
        "external_id",
        "stage_external_id",
        "from_stage_external_id",
        "data_payload",
    )
    _drop_columns(
        "crm_activities",
        "external_id",
        "related_external_id",
        "deal_external_id",
        "contact_external_id",
        "company_external_id",
        "owner_external_id",
        "data_payload",
    )
    _drop_columns(
        "crm_object_associations",
        "external_id",
        "from_external_id",
        "to_external_id",
        "data_payload",
    )
    _drop_columns("crm_leads", "external_id", "owner_external_id", "data_payload")
    _drop_columns(
        "crm_lead_participants",
        "external_id",
        "related_external_id",
        "data_payload",
    )
    _drop_constraints("crm_lead_participants", "ck_crm_lead_participant_has_identity")
    op.execute(
        """
        ALTER TABLE crm_lead_participants
        ADD CONSTRAINT ck_crm_lead_participant_has_identity
        CHECK (related_local_id IS NOT NULL OR email IS NOT NULL)
        """
    )

    op.execute(
        """
        ALTER TABLE crm_deal_participants
        ADD CONSTRAINT uq_crm_deal_participant_unique
        UNIQUE (deal_id, participant_type, related_local_id, relationship_role)
        """
    )
    op.execute(
        """
        ALTER TABLE crm_object_associations
        ADD CONSTRAINT uq_crm_object_association_logical
        UNIQUE (
            integration_id,
            from_object_type,
            from_local_id,
            to_object_type,
            to_local_id,
            association_type,
            role_label
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_deal_participant_related
        ON crm_deal_participants (related_local_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_activity_related
        ON crm_activities (organisation_id, related_object_type, related_local_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_assoc_from_object
        ON crm_object_associations (from_object_type, from_local_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crm_assoc_to_object
        ON crm_object_associations (to_object_type, to_local_id)
        """
    )


def downgrade() -> None:
    """Recreate dropped columns for structural rollback only."""
    op.execute("ALTER TABLE crm_companies ADD COLUMN IF NOT EXISTS external_id varchar(255)")
    op.execute("ALTER TABLE crm_companies ADD COLUMN IF NOT EXISTS owner_external_id varchar(255)")
    op.execute("ALTER TABLE crm_companies ADD COLUMN IF NOT EXISTS data_payload jsonb")

    op.execute("ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS external_id varchar(255)")
    op.execute("ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS company_external_id varchar(255)")
    op.execute("ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS owner_external_id varchar(255)")
    op.execute("ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS data_payload jsonb")

    op.execute("ALTER TABLE crm_users ADD COLUMN IF NOT EXISTS external_id varchar(255)")
    op.execute("ALTER TABLE crm_users ADD COLUMN IF NOT EXISTS data_payload jsonb")

    op.execute("ALTER TABLE crm_pipelines ADD COLUMN IF NOT EXISTS external_id varchar(255)")
    op.execute("ALTER TABLE crm_pipelines ADD COLUMN IF NOT EXISTS data_payload jsonb")
    op.execute("ALTER TABLE crm_deal_stages ADD COLUMN IF NOT EXISTS external_id varchar(255)")
    op.execute("ALTER TABLE crm_deal_stages ADD COLUMN IF NOT EXISTS data_payload jsonb")

    for table, columns in {
        "crm_deals": [
            "external_id varchar(255)",
            "stage_external_id varchar(255)",
            "pipeline_external_id varchar(255)",
            "owner_external_id varchar(255)",
            "created_by_user_external_id varchar(255)",
            "updated_by_user_external_id varchar(255)",
            "company_external_id varchar(255)",
            "primary_contact_external_id varchar(255)",
            "associations_payload jsonb",
            "data_payload jsonb",
        ],
        "crm_deal_participants": [
            "external_id varchar(255)",
            "related_external_id varchar(255)",
            "data_payload jsonb",
        ],
        "crm_deal_stage_history": [
            "external_id varchar(255)",
            "stage_external_id varchar(255)",
            "from_stage_external_id varchar(255)",
            "data_payload jsonb",
        ],
        "crm_activities": [
            "external_id varchar(255)",
            "related_external_id varchar(255)",
            "deal_external_id varchar(255)",
            "contact_external_id varchar(255)",
            "company_external_id varchar(255)",
            "owner_external_id varchar(255)",
            "data_payload jsonb",
        ],
        "crm_object_associations": [
            "external_id varchar(255)",
            "from_external_id varchar(255)",
            "to_external_id varchar(255)",
            "data_payload jsonb",
        ],
        "crm_leads": [
            "external_id varchar(255)",
            "owner_external_id varchar(255)",
            "data_payload jsonb",
        ],
        "crm_lead_participants": [
            "external_id varchar(255)",
            "related_external_id varchar(255)",
            "data_payload jsonb",
        ],
    }.items():
        for column in columns:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}")
