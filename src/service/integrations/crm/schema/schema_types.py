"""Provider-neutral types for schema sync.

These classes intentionally avoid ORM/session concerns so the sync pipeline can
run pure classification + normalization before persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
)


@dataclass(slots=True, frozen=True)
class CanonicalMatch:
    """HubSpot field that is already represented on CRMDeal."""

    hubspot_property: str
    canonical_column: str


@dataclass(slots=True, frozen=True)
class ProviderCustomMatch:
    """HubSpot field that is not canonical and can be stored as provider custom."""

    hubspot_property: str
    is_provider_custom: bool


@dataclass(slots=True, frozen=True)
class IgnoredMatch:
    """HubSpot field that should be intentionally dropped during schema sync."""

    hubspot_property: str


Classification = CanonicalMatch | ProviderCustomMatch | IgnoredMatch


@dataclass(slots=True)
class NormalizedSchemaOption:
    """Normalized select/list option shape used by `CRMCustomFieldDefinition.options`."""

    value: str
    label: str
    description: str | None = None
    display_order: int | None = None
    hidden: bool = False


@dataclass(slots=True)
class NormalizedSchemaField:
    """Provider field after normalization, ready for registry persistence."""

    provider_field_key: str
    key: str
    label: str
    description: str | None
    data_type: CRMCustomFieldDataType
    is_provider_custom: bool
    display_order: int | None = None
    options: list[NormalizedSchemaOption] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    formula: dict[str, Any] | None = None
    data_payload: dict[str, Any] = field(default_factory=dict)
    source_modified_at: datetime | None = None


@dataclass(slots=True)
class RegistryWriteResult:
    """Counters and rows returned from an apply cycle."""

    created: int = 0
    updated: int = 0
    reactivated: int = 0
    deactivated: int = 0
    rows: list[CRMCustomFieldDefinition] = field(default_factory=list)


@dataclass(slots=True)
class SchemaSyncResult:
    """Sync summary that feeds both sync history and integration metadata."""

    provider: str
    object_type: str
    organisation_id: UUID
    integration_id: UUID
    fetched_properties: int
    canonical_properties: int
    custom_properties_persisted: int
    created: int
    updated: int
    reactivated: int
    deactivated: int
    skipped: int
    started_at: datetime
    completed_at: datetime

    def to_metadata(self) -> dict[str, Any]:
        """Render a compact metadata payload for sync observability."""

        return {
            "provider": self.provider,
            "object_type": self.object_type,
            "fetched_properties": self.fetched_properties,
            "canonical_properties": self.canonical_properties,
            "custom_properties_persisted": self.custom_properties_persisted,
            "created": self.created,
            "updated": self.updated,
            "reactivated": self.reactivated,
            "deactivated": self.deactivated,
            "skipped": self.skipped,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
        }
