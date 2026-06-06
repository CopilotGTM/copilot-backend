from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import TimestampMixin, utcnow
from ..integration_models import EMPTY_JSONB_SQL


class CRMSourceKind(StrEnum):
    INTEGRATION = "integration"
    MIGRATION = "migration"


class CRMSourceObjectType(StrEnum):
    DEAL = "deal"
    CONTACT = "contact"
    COMPANY = "company"
    USER = "user"
    PIPELINE = "pipeline"
    STAGE = "stage"
    ACTIVITY = "activity"
    NOTE = "note"
    TASK = "task"
    ASSOCIATION = "association"
    LEAD = "lead"
    LEAD_PARTICIPANT = "lead_participant"
    DEAL_PARTICIPANT = "deal_participant"
    STAGE_HISTORY = "stage_history"
    NOTE_LINK = "note_link"


_CRM_SOURCE_OBJECT_TYPES_SQL = (
    "'deal', 'contact', 'company', 'user', 'pipeline', 'stage', 'activity', "
    "'note', 'task', 'association', 'lead', 'lead_participant', "
    "'deal_participant', 'stage_history', 'note_link'"
)


class CRMSourceObjectMapping(TimestampMixin, table=True):
    """Source identity mapping between provider records and local CRM objects."""

    __tablename__ = "crm_source_object_mappings"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('integration', 'migration')",
            name="ck_crm_source_mapping_source_kind",
        ),
        CheckConstraint(
            "("
            "source_kind = 'integration' "
            "AND integration_id IS NOT NULL "
            "AND source_run_id IS NULL"
            ") OR ("
            "source_kind = 'migration' "
            "AND source_run_id IS NOT NULL "
            "AND integration_id IS NULL"
            ")",
            name="ck_crm_source_mapping_source_ref",
        ),
        CheckConstraint(
            f"external_object_type IN ({_CRM_SOURCE_OBJECT_TYPES_SQL})",
            name="ck_crm_source_mapping_external_type",
        ),
        CheckConstraint(
            f"local_object_type IN ({_CRM_SOURCE_OBJECT_TYPES_SQL})",
            name="ck_crm_source_mapping_local_type",
        ),
        CheckConstraint(
            "length(btrim(provider)) > 0 AND length(btrim(external_id)) > 0",
            name="ck_crm_source_mapping_non_empty_identity",
        ),
        CheckConstraint(
            "provider = lower(btrim(provider))",
            name="ck_crm_source_mapping_provider_normalized",
        ),
        CheckConstraint(
            "jsonb_typeof(mapping_metadata) = 'object'",
            name="ck_crm_source_mapping_metadata_object",
        ),
        Index(
            "uq_crm_source_mapping_integration_identity",
            "organisation_id",
            "integration_id",
            "provider",
            "external_object_type",
            "external_id",
            unique=True,
            postgresql_where=text("source_kind = 'integration'"),
        ),
        Index(
            "uq_crm_source_mapping_migration_identity",
            "organisation_id",
            "source_run_id",
            "provider",
            "external_object_type",
            "external_id",
            unique=True,
            postgresql_where=text("source_kind = 'migration'"),
        ),
        Index(
            "ix_crm_source_mapping_source_lookup",
            "organisation_id",
            "provider",
            "external_object_type",
            "external_id",
        ),
        Index(
            "ix_crm_source_mapping_local_lookup",
            "organisation_id",
            "local_object_type",
            "local_object_id",
        ),
        Index(
            "ix_crm_source_mapping_integration",
            "organisation_id",
            "integration_id",
        ),
        Index("ix_crm_source_mapping_active", "organisation_id", "is_deleted"),
    )

    mapping_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    source_kind: CRMSourceKind = Field(
        sa_column=Column(String(16), nullable=False),
        description="Whether this identity came from a live integration or a migration/import run.",
    )
    integration_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid,
            sa.ForeignKey("knowledge_integrations.integration_id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        description="Owning integration for live provider mappings. Null for migration mappings.",
    )
    source_run_id: UUID | None = Field(
        default=None,
        index=True,
        description="Migration/import run identifier. Null for live integration mappings.",
    )
    provider: str = Field(
        sa_column=Column(String(64), nullable=False),
        description="Source provider name, for example hubspot or salesforce.",
    )
    external_object_type: CRMSourceObjectType = Field(
        sa_column=Column(String(64), nullable=False),
        description="Provider object type represented by external_id.",
    )
    external_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Provider or migration source identifier for the external object.",
    )
    local_object_type: CRMSourceObjectType = Field(
        sa_column=Column(String(64), nullable=False),
        description="Canonical local CRM object type targeted by this mapping.",
    )
    local_object_id: UUID = Field(
        index=True,
        description="Canonical local CRM row ID. Polymorphic by local_object_type.",
    )
    data_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Raw source payload for replay and troubleshooting.",
    )
    mapping_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional source diagnostics or unresolved relationship details.",
    )
    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Created timestamp reported by the source system.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp reported by the source system.",
    )
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
        description="Time this mapping was last synced into CopilotGTM.",
    )
    last_seen_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent sync time where this source record was observed.",
    )
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
        description="Soft-delete marker; source identity remains unique across deleted mappings.",
    )
