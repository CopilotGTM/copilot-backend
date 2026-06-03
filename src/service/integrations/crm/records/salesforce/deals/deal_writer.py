"""Upsert Salesforce Opportunity rows into crm_deals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    DealResolutionMaps,
    DealWriteResult,
    DimensionHit,
    NormalizedSalesforceOpportunity,
)

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class _ResolvedFks:
    pipeline_id: UUID | None
    pipeline_name: str | None
    pipeline_label: str | None
    stage_id: UUID | None
    stage_name: str | None
    stage_label: str | None
    owner_id: UUID | None
    owner_name: str | None
    owner_email: str | None
    created_by_user_id: UUID | None
    updated_by_user_id: UUID | None
    company_id: UUID | None
    company_name: str | None
    company_domain: str | None
    primary_contact_id: UUID | None
    primary_contact_name: str | None
    primary_contact_email: str | None


class SalesforceDealWriter:
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
        normalized: list[NormalizedSalesforceOpportunity],
        maps: DealResolutionMaps,
        preserve_relationship_fields: bool = False,
    ) -> DealWriteResult:
        result = DealWriteResult()
        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows(
            external_ids={deal.external_id for deal in normalized}
        )

        for deal in normalized:
            resolved = self._resolve_all(deal, maps, result)
            existing = existing_by_external.get(deal.external_id)
            if existing is None:
                row = self._new_row(deal, resolved, now=now)
                self.session.add(row)
                existing_by_external[deal.external_id] = row
                result.deals_created += 1
            else:
                self._apply_updates(
                    existing,
                    deal,
                    resolved,
                    now=now,
                    preserve_relationship_fields=preserve_relationship_fields,
                )
                result.deals_updated += 1

        await self.session.flush()
        logger.info(
            "Salesforce deal writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "deals_created": result.deals_created,
                "deals_updated": result.deals_updated,
                "unresolved_pipeline_link": result.unresolved_pipeline_link,
                "unresolved_stage_link": result.unresolved_stage_link,
                "unresolved_owner_link": result.unresolved_owner_link,
                "unresolved_company_link": result.unresolved_company_link,
                "unresolved_contact_link": result.unresolved_contact_link,
                "unresolved_created_by_link": result.unresolved_created_by_link,
                "unresolved_updated_by_link": result.unresolved_updated_by_link,
            },
        )
        return result

    def _resolve_all(
        self,
        deal: NormalizedSalesforceOpportunity,
        maps: DealResolutionMaps,
        result: DealWriteResult,
    ) -> _ResolvedFks:
        pipeline_hit = _lookup(deal.pipeline_external_id, maps.pipeline_map)
        if deal.pipeline_external_id and pipeline_hit is None:
            result.unresolved_pipeline_link += 1

        stage_hit = _lookup(deal.stage_external_id, maps.stage_map)
        if deal.stage_external_id and stage_hit is None:
            result.unresolved_stage_link += 1

        owner_hit = _lookup(deal.owner_external_id, maps.user_map)
        if deal.owner_external_id and owner_hit is None:
            result.unresolved_owner_link += 1

        created_by_hit = _lookup(deal.created_by_user_external_id, maps.user_map)
        if deal.created_by_user_external_id and created_by_hit is None:
            result.unresolved_created_by_link += 1

        updated_by_hit = _lookup(deal.updated_by_user_external_id, maps.user_map)
        if deal.updated_by_user_external_id and updated_by_hit is None:
            result.unresolved_updated_by_link += 1

        company_hit = _lookup(deal.primary_company_external_id, maps.company_map)
        if deal.primary_company_external_id and company_hit is None:
            result.unresolved_company_link += 1

        contact_hit = _lookup(deal.primary_contact_external_id, maps.contact_map)
        if deal.primary_contact_external_id and contact_hit is None:
            result.unresolved_contact_link += 1

        pipeline_id_from_stage = (
            stage_hit.pipeline_id if pipeline_hit is None and stage_hit is not None else None
        )

        return _ResolvedFks(
            pipeline_id=(
                pipeline_hit.internal_id if pipeline_hit is not None else pipeline_id_from_stage
            ),
            pipeline_name=pipeline_hit.name if pipeline_hit is not None else None,
            pipeline_label=pipeline_hit.label if pipeline_hit is not None else None,
            stage_id=stage_hit.internal_id if stage_hit is not None else None,
            stage_name=stage_hit.name if stage_hit is not None else None,
            stage_label=stage_hit.label if stage_hit is not None else None,
            owner_id=owner_hit.internal_id if owner_hit is not None else None,
            owner_name=owner_hit.name if owner_hit is not None else None,
            owner_email=owner_hit.email if owner_hit is not None else None,
            created_by_user_id=(created_by_hit.internal_id if created_by_hit is not None else None),
            updated_by_user_id=(updated_by_hit.internal_id if updated_by_hit is not None else None),
            company_id=company_hit.internal_id if company_hit is not None else None,
            company_name=company_hit.name if company_hit is not None else None,
            company_domain=company_hit.domain if company_hit is not None else None,
            primary_contact_id=contact_hit.internal_id if contact_hit is not None else None,
            primary_contact_name=contact_hit.name if contact_hit is not None else None,
            primary_contact_email=contact_hit.email if contact_hit is not None else None,
        )

    async def _load_existing_rows(self, external_ids: set[str] | None = None) -> dict[str, CRMDeal]:
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.integration_id) == self.integration_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
        )
        if external_ids is not None:
            if not external_ids:
                return {}
            stmt = stmt.where(col(CRMDeal.external_id).in_(external_ids))
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.external_id: row for row in rows if row.external_id is not None}

    def _new_row(
        self,
        deal: NormalizedSalesforceOpportunity,
        resolved: _ResolvedFks,
        *,
        now: datetime,
    ) -> CRMDeal:
        return CRMDeal(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            external_id=deal.external_id,
            name=deal.name,
            amount=deal.amount,
            currency=deal.currency,
            amount_home_currency=deal.amount_home_currency,
            deal_status=deal.deal_status,
            probability=deal.probability,
            predicted_amount=deal.predicted_amount,
            close_date=deal.close_date,
            expected_close_date=deal.expected_close_date,
            closed_won_at=deal.closed_won_at,
            closed_lost_at=deal.closed_lost_at,
            closed_reason=deal.closed_reason,
            crm_url=deal.crm_url,
            stage_external_id=deal.stage_external_id,
            stage_name=resolved.stage_name,
            stage_label=resolved.stage_label,
            pipeline_external_id=deal.pipeline_external_id,
            pipeline_name=resolved.pipeline_name,
            pipeline_label=resolved.pipeline_label,
            pipeline_id=resolved.pipeline_id,
            stage_id=resolved.stage_id,
            forecast_category=deal.forecast_category,
            deal_type=deal.deal_type,
            priority=deal.priority,
            lead_source=deal.lead_source,
            owner_external_id=deal.owner_external_id,
            owner_name=resolved.owner_name,
            owner_email=resolved.owner_email,
            owner_id=resolved.owner_id,
            created_by_user_id=resolved.created_by_user_id,
            updated_by_user_id=resolved.updated_by_user_id,
            created_by_user_external_id=deal.created_by_user_external_id,
            updated_by_user_external_id=deal.updated_by_user_external_id,
            company_external_id=deal.primary_company_external_id,
            company_id=resolved.company_id,
            company_name=resolved.company_name,
            company_domain=resolved.company_domain,
            primary_contact_external_id=deal.primary_contact_external_id,
            primary_contact_id=resolved.primary_contact_id,
            primary_contact_name=resolved.primary_contact_name,
            primary_contact_email=resolved.primary_contact_email,
            source_created_at=deal.source_created_at,
            last_activity_at=deal.last_activity_at,
            days_to_close=deal.days_to_close,
            stage_entered_at=deal.stage_entered_at,
            stage_updated_at=deal.stage_updated_at,
            synced_at=now,
            source_modified_at=deal.source_modified_at,
            last_seen_at=now,
            description=deal.description,
            properties=deal.properties,
            metrics=deal.metrics,
            associated_contact_count=deal.associated_contact_count,
            associated_company_count=deal.associated_company_count,
            note_count=deal.note_count,
            associations_payload=deal.associations_payload,
            data_payload=deal.data_payload,
        )

    def _apply_updates(
        self,
        row: CRMDeal,
        deal: NormalizedSalesforceOpportunity,
        resolved: _ResolvedFks,
        *,
        now: datetime,
        preserve_relationship_fields: bool = False,
    ) -> None:
        row.name = deal.name
        row.amount = deal.amount
        row.currency = deal.currency
        row.amount_home_currency = deal.amount_home_currency
        row.deal_status = deal.deal_status
        row.probability = deal.probability
        row.predicted_amount = deal.predicted_amount
        row.close_date = deal.close_date
        row.expected_close_date = deal.expected_close_date
        row.closed_won_at = deal.closed_won_at
        row.closed_lost_at = deal.closed_lost_at
        row.closed_reason = deal.closed_reason
        row.crm_url = deal.crm_url
        row.stage_external_id = deal.stage_external_id
        row.stage_name = resolved.stage_name
        row.stage_label = resolved.stage_label
        row.pipeline_external_id = deal.pipeline_external_id
        row.pipeline_name = resolved.pipeline_name
        row.pipeline_label = resolved.pipeline_label
        row.pipeline_id = resolved.pipeline_id
        row.stage_id = resolved.stage_id
        row.forecast_category = deal.forecast_category
        row.deal_type = deal.deal_type
        row.priority = deal.priority
        row.lead_source = deal.lead_source
        row.owner_external_id = deal.owner_external_id
        row.owner_name = resolved.owner_name
        row.owner_email = resolved.owner_email
        row.owner_id = resolved.owner_id
        row.created_by_user_id = resolved.created_by_user_id
        row.updated_by_user_id = resolved.updated_by_user_id
        row.created_by_user_external_id = deal.created_by_user_external_id
        row.updated_by_user_external_id = deal.updated_by_user_external_id
        row.company_external_id = deal.primary_company_external_id
        row.company_id = resolved.company_id
        row.company_name = resolved.company_name
        row.company_domain = resolved.company_domain
        if not preserve_relationship_fields or deal.primary_contact_external_id is not None:
            row.primary_contact_external_id = deal.primary_contact_external_id
            row.primary_contact_id = resolved.primary_contact_id
            row.primary_contact_name = resolved.primary_contact_name
            row.primary_contact_email = resolved.primary_contact_email
        row.source_created_at = deal.source_created_at
        row.last_activity_at = deal.last_activity_at
        row.days_to_close = deal.days_to_close
        if not preserve_relationship_fields or deal.stage_entered_at is not None:
            row.stage_entered_at = deal.stage_entered_at
        row.stage_updated_at = deal.stage_updated_at
        row.synced_at = now
        row.source_modified_at = deal.source_modified_at
        row.last_seen_at = now
        row.description = deal.description
        row.properties = deal.properties
        row.metrics = deal.metrics
        row.associated_contact_count = deal.associated_contact_count
        row.associated_company_count = deal.associated_company_count
        row.note_count = deal.note_count
        row.associations_payload = deal.associations_payload
        row.data_payload = deal.data_payload


def _lookup(key: str | None, mapping: dict[str, DimensionHit]) -> DimensionHit | None:
    if key is None:
        return None
    return mapping.get(key)
