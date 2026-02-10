from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, Column, Index, String, UniqueConstraint, text
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
    BOOLEAN = "boolean"
    SELECT = "select"
    MULTISELECT = "multiselect"
    FORMULA = "formula"


class CRMCustomFieldDefinition(TimestampMixin, table=True):
    """Organisation-scoped schema definition for custom CRM fields."""

    __tablename__ = "crm_custom_field_definitions"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "entity_type",
            "key",
            "is_deleted",
            name="uq_crm_custom_field_org_entity_key_state",
        ),
        Index(
            "ix_crm_custom_field_org_entity_active",
            "organisation_id",
            "entity_type",
            "is_active",
            "is_deleted",
        ),
        CheckConstraint(
            "key ~ '^[a-z][a-z0-9_]{1,62}$'",
            name="ck_crm_custom_field_key_format",
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
    key: str = Field(
        sa_column=Column(String(64), nullable=False),
        description="Stable machine key used in payloads, CSV mapping, and properties storage.",
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
