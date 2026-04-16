from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import TimestampMixin
from ..integration_models import EMPTY_JSONB_SQL


class CRMCustomFieldEntity(str, Enum):
    """Supported entity scopes for custom fields."""

    DEAL = "deal"


class CRMCustomFieldDataType(str, Enum):
    """Supported value types for custom fields."""

    TEXT = "text"
    INTEGER = "integer"
    NUMBER = "number"
    DATE = "date"
    DATETIME = "datetime"
    CURRENCY = "currency"
    PERCENTAGE = "percentage"
    BOOLEAN = "boolean"
    SELECT = "select"
    MULTISELECT = "multiselect"
    FORMULA = "formula"


class CRMCustomFieldSource(str, Enum):
    """Where a custom-field definition originates."""

    INTERNAL = "internal"
    PROVIDER = "provider"


class CRMCustomFieldDefinition(TimestampMixin, table=True):
    """Organisation-scoped schema definition for custom CRM fields."""

    __tablename__ = "crm_custom_field_definitions"
    __table_args__ = (
        Index(
            "uq_crm_custom_field_internal_key_state",
            "organisation_id",
            "entity_type",
            "key",
            "is_deleted",
            unique=True,
            postgresql_where=text("field_source = 'internal'"),
        ),
        Index(
            "uq_crm_custom_field_provider_key_state",
            "integration_id",
            "entity_type",
            "provider_field_key",
            "is_deleted",
            unique=True,
            postgresql_where=text("field_source = 'provider'"),
        ),
        Index(
            "ix_crm_custom_field_org_entity_active",
            "organisation_id",
            "entity_type",
            "field_source",
            "is_active",
            "is_deleted",
        ),
        Index(
            "ix_crm_custom_field_integration_entity_active",
            "integration_id",
            "entity_type",
            "is_active",
            "is_deleted",
        ),
        CheckConstraint(
            "key ~ '^[a-z][a-z0-9_]{1,62}$'",
            name="ck_crm_custom_field_key_format",
        ),
        CheckConstraint(
            "("
            "field_source = 'internal' "
            "AND integration_id IS NULL "
            "AND provider_name IS NULL "
            "AND provider_field_key IS NULL "
            "AND is_provider_custom IS NULL"
            ") OR ("
            "field_source = 'provider' "
            "AND integration_id IS NOT NULL "
            "AND provider_name IS NOT NULL "
            "AND provider_field_key IS NOT NULL"
            ")",
            name="ck_crm_custom_field_source_provenance",
        ),
    )

    field_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for a custom field definition row.",
    )
    organisation_id: UUID = Field(
        index=True,
        description="Owning organisation; keeps field definitions tenant-isolated.",
    )
    entity_type: CRMCustomFieldEntity = Field(
        default=CRMCustomFieldEntity.DEAL,
        sa_column=Column(String(32), nullable=False, index=True),
        description="CRM entity this definition applies to (currently deal).",
    )
    field_source: CRMCustomFieldSource = Field(
        default=CRMCustomFieldSource.INTERNAL,
        sa_column=Column(
            String(16),
            nullable=False,
            server_default=text("'internal'"),
            index=True,
        ),
        description="Whether the definition is owned by our internal CRM or mirrored from a provider.",
    )
    integration_id: UUID | None = Field(
        default=None,
        foreign_key="knowledge_integrations.integration_id",
        index=True,
        description="Owning integration for provider-backed field definitions. Null for internal fields.",
    )
    provider_name: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Provider identifier for mirrored field definitions (for example hubspot).",
    )
    key: str = Field(
        sa_column=Column(String(64), nullable=False),
        description="Stable machine key used in payloads, CSV mapping, and properties storage.",
    )
    provider_field_key: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Provider-native field key or property name for mirrored schema rows.",
    )
    label: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Human-readable field name shown in UI and import mapping prompts.",
    )
    data_type: CRMCustomFieldDataType = Field(
        default=CRMCustomFieldDataType.TEXT,
        sa_column=Column(String(32), nullable=False),
        description="Value type used for parsing, validation, and casting during import/update.",
    )
    required: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
        description="Whether this field must be present when creating or updating records.",
    )
    is_predefined: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
        description="Marks system-provided fields that should not be removed by tenant cleanup.",
    )
    is_provider_custom: bool | None = Field(
        default=None,
        sa_column=Column(Boolean, nullable=True),
        description="Whether the external provider marks this field as custom rather than system-defined.",
    )
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
        description="Controls whether this field is available in UI, import, and write flows.",
    )
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
        description="Soft-delete flag to preserve history while excluding field from active use.",
    )
    display_order: int | None = Field(
        default=None,
        description="Optional UI ordering hint for rendering custom fields consistently.",
    )
    description: str | None = Field(
        default=None,
        sa_column=Column(String(500), nullable=True),
        description="Optional admin-facing explanation of business meaning and usage.",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Type-specific config (for example select options) used by UI and validators.",
    )
    validation: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Additional validation rules (ranges/patterns) enforced during ingestion.",
    )
    formula: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Formula metadata for computed fields when formula evaluation is supported.",
    )
    data_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Raw provider schema payload for mirrored fields, kept for replay and troubleshooting.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp reported by the provider for this field definition.",
    )
    last_seen_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this schema row was observed during provider sync.",
    )
