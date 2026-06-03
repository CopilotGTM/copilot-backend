"""Upsert Salesforce deal associations into crm_object_associations."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.associations import (
    CRMAssociationCategory,
    CRMObjectAssociation,
    CRMObjectType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    AssociationWriteResult,
    DimensionHit,
    NormalizedSalesforceDealAssociation,
)

logger = get_logger(__name__)


class SalesforceDealAssociationWriter:
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
        edges: list[NormalizedSalesforceDealAssociation],
        deal_pk_map: dict[str, UUID],
        contact_map: dict[str, DimensionHit],
        company_map: dict[str, DimensionHit],
    ) -> AssociationWriteResult:
        result = AssociationWriteResult()
        if not edges:
            await self.session.flush()
            return result

        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows(
            external_ids={_synthetic_external_id(edge) for edge in edges}
        )

        for edge in edges:
            from_local_id = deal_pk_map.get(edge.from_external_id)
            if from_local_id is None:
                continue

            to_local_id = self._resolve_to_local(
                edge=edge,
                contact_map=contact_map,
                company_map=company_map,
            )
            if to_local_id is None:
                result.unresolved_to_local += 1

            external_id = _synthetic_external_id(edge)
            existing = existing_by_external.get(external_id)
            if existing is None:
                row = self._new_row(
                    edge=edge,
                    external_id=external_id,
                    from_local_id=from_local_id,
                    to_local_id=to_local_id,
                    now=now,
                )
                self.session.add(row)
                existing_by_external[external_id] = row
                result.associations_created += 1
            else:
                self._apply_updates(
                    existing,
                    edge=edge,
                    from_local_id=from_local_id,
                    to_local_id=to_local_id,
                    now=now,
                )
                result.associations_updated += 1

        await self.session.flush()
        logger.info(
            "Salesforce association writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "associations_created": result.associations_created,
                "associations_updated": result.associations_updated,
                "unresolved_to_local": result.unresolved_to_local,
            },
        )
        return result

    def _resolve_to_local(
        self,
        *,
        edge: NormalizedSalesforceDealAssociation,
        contact_map: dict[str, DimensionHit],
        company_map: dict[str, DimensionHit],
    ) -> UUID | None:
        if edge.to_object_type == CRMObjectType.CONTACT.value:
            hit = contact_map.get(edge.to_external_id)
            return hit.internal_id if hit is not None else None
        if edge.to_object_type == CRMObjectType.COMPANY.value:
            hit = company_map.get(edge.to_external_id)
            return hit.internal_id if hit is not None else None
        return None

    async def _load_existing_rows(
        self,
        external_ids: set[str] | None = None,
    ) -> dict[str, CRMObjectAssociation]:
        stmt = select(CRMObjectAssociation).where(
            col(CRMObjectAssociation.organisation_id) == self.organisation_id,
            col(CRMObjectAssociation.integration_id) == self.integration_id,
            col(CRMObjectAssociation.is_deleted) == False,  # noqa: E712
            col(CRMObjectAssociation.from_object_type) == CRMObjectType.DEAL,
        )
        if external_ids is not None:
            if not external_ids:
                return {}
            stmt = stmt.where(col(CRMObjectAssociation.external_id).in_(external_ids))
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.external_id: row for row in rows if row.external_id is not None}

    def _new_row(
        self,
        *,
        edge: NormalizedSalesforceDealAssociation,
        external_id: str,
        from_local_id: UUID,
        to_local_id: UUID | None,
        now: datetime,
    ) -> CRMObjectAssociation:
        return CRMObjectAssociation(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            external_id=external_id,
            from_object_type=CRMObjectType(edge.from_object_type),
            from_external_id=edge.from_external_id,
            from_local_id=from_local_id,
            to_object_type=CRMObjectType(edge.to_object_type),
            to_external_id=edge.to_external_id,
            to_local_id=to_local_id,
            association_category=CRMAssociationCategory(edge.association_category),
            association_type_id=edge.association_type_id,
            association_label=edge.association_label,
            role_label=edge.role_label,
            is_primary=edge.is_primary,
            synced_at=now,
            last_seen_at=now,
            data_payload=edge.data_payload,
        )

    def _apply_updates(
        self,
        row: CRMObjectAssociation,
        *,
        edge: NormalizedSalesforceDealAssociation,
        from_local_id: UUID,
        to_local_id: UUID | None,
        now: datetime,
    ) -> None:
        row.from_local_id = from_local_id
        row.to_local_id = to_local_id
        row.association_category = CRMAssociationCategory(edge.association_category)
        row.association_type_id = edge.association_type_id
        row.association_label = edge.association_label
        row.role_label = edge.role_label
        row.is_primary = edge.is_primary
        row.synced_at = now
        row.last_seen_at = now
        row.data_payload = edge.data_payload


def _synthetic_external_id(edge: NormalizedSalesforceDealAssociation) -> str:
    type_id = edge.association_type_id or ""
    return (
        f"{edge.from_external_id}:{edge.from_object_type}:"
        f"{edge.to_object_type}:{edge.to_external_id}:{type_id}"
    )
