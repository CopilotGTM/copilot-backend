"""Provider-backed persistence for HubSpot deal schema sync.

Owns all writes to crm_custom_field_definitions where field_source='provider'
for the deal entity scope. Strictly refuses to touch field_source='internal'
rows. Upserts active rows and deactivates previously-seen rows that are missing
from the current sync payload (active-snapshot semantics).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.schema.normalizer import options_payload
from src.service.integrations.crm.schema.schema_types import (
    NormalizedSchemaField,
    RegistryWriteResult,
)

logger = get_logger(__name__)


class HubSpotDealRegistryWriter:
    """Persist provider-backed HubSpot deal schema rows for a single integration.

    Scope stays strictly within provider-backed rows so internal/custom columns stay
    owned by their own lifecycle.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id

    async def apply(
        self,
        *,
        fields: list[NormalizedSchemaField],
    ) -> RegistryWriteResult:
        """Apply one sync pass with upsert + active-snapshot deactivation semantics."""

        existing = await self._load_existing()
        now = datetime.now(UTC)

        result = RegistryWriteResult()
        incoming_keys: set[str] = set()

        for nf in fields:
            incoming_keys.add(nf.provider_field_key)
            row = existing.get(nf.provider_field_key)
            if row is None:
                new_row = self._build_row(nf, now=now)
                self.session.add(new_row)
                result.rows.append(new_row)
                result.created += 1
                continue

            was_active = bool(row.is_active)
            self._apply_fields(row, nf, now=now)
            row.is_active = True
            result.rows.append(row)
            if was_active:
                result.updated += 1
            else:
                result.reactivated += 1

        for key, row in existing.items():
            if key in incoming_keys:
                continue
            if row.is_active:
                row.is_active = False
                result.deactivated += 1

        await self.session.flush()
        # Prefix counter keys with "rows_" — Python's LogRecord reserves "created"
        # as the log-record timestamp; passing a bare "created" key via extra=
        # raises KeyError on overwrite.
        logger.info(
            "HubSpot deal registry write applied",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "rows_created": result.created,
                "rows_updated": result.updated,
                "rows_reactivated": result.reactivated,
                "rows_deactivated": result.deactivated,
            },
        )
        return result

    async def _load_existing(self) -> dict[str, CRMCustomFieldDefinition]:
        """Load current provider-backed active and inactive fields for this org/integration."""

        stmt = select(CRMCustomFieldDefinition).where(
            col(CRMCustomFieldDefinition.organisation_id) == self.organisation_id,
            col(CRMCustomFieldDefinition.integration_id) == self.integration_id,
            col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
            col(CRMCustomFieldDefinition.field_source) == CRMCustomFieldSource.PROVIDER.value,
            col(CRMCustomFieldDefinition.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        rows = result.scalars().all()
        out: dict[str, CRMCustomFieldDefinition] = {}
        for row in rows:
            if row.provider_field_key is None:
                continue
            out[row.provider_field_key] = row
        return out

    def _build_row(
        self,
        nf: NormalizedSchemaField,
        *,
        now: datetime,
    ) -> CRMCustomFieldDefinition:
        """Create a new provider-backed field definition row from normalized data."""

        return CRMCustomFieldDefinition(
            organisation_id=self.organisation_id,
            entity_type=CRMCustomFieldEntity.DEAL,
            field_source=CRMCustomFieldSource.PROVIDER,
            integration_id=self.integration_id,
            provider_name="hubspot",
            key=nf.key,
            provider_field_key=nf.provider_field_key,
            label=nf.label,
            description=nf.description,
            data_type=nf.data_type,
            required=False,
            is_predefined=False,
            is_provider_custom=nf.is_provider_custom,
            display_order=nf.display_order,
            options=options_payload(nf.options),
            validation=nf.validation or {},
            formula=nf.formula,
            data_payload=nf.data_payload or None,
            source_modified_at=nf.source_modified_at,
            last_seen_at=now,
            is_active=True,
            is_deleted=False,
        )

    def _apply_fields(
        self,
        row: CRMCustomFieldDefinition,
        nf: NormalizedSchemaField,
        *,
        now: datetime,
    ) -> None:
        """Mutate an existing registry row without changing tenant integration identity."""

        row.key = nf.key
        row.label = nf.label
        row.description = nf.description
        row.data_type = nf.data_type
        row.is_provider_custom = nf.is_provider_custom
        row.display_order = nf.display_order
        row.options = options_payload(nf.options)
        row.validation = nf.validation or {}
        row.formula = nf.formula
        row.data_payload = nf.data_payload or None
        row.source_modified_at = nf.source_modified_at
        row.last_seen_at = now
