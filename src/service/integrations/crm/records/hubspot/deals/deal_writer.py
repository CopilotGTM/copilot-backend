"""Single-pass upsert writer for CRMDeal rows from HubSpot deals.

Scope filter on every statement: `(organisation_id, integration_id)`.
Resolves seven FKs at write time using `DealResolutionMaps`
loaded by the orchestrator before construction:

  * `pipeline_id`         ← `pipeline_external_id` via `pipeline_map`
  * `stage_id`            ← `stage_external_id`    via `stage_map`
  * `owner_id`            ← `owner_external_id`    via `user_map`
  * `created_by_user_id`  ← `created_by_user_external_id` via `user_map`
  * `updated_by_user_id`  ← `updated_by_user_external_id` via `user_map`
  * `company_id`          ← primary association id via `company_map`
  * `primary_contact_id`  ← primary association id via `contact_map`

Misses leave the FK NULL and tick the appropriate counter. We never fail the
run on an unresolved link — the row still lands, and a future sync can resolve it.

There is no stale-deactivation pass. Deals that disappear from a full HubSpot
snapshot are left untouched intentionally; explicit deletion webhooks can
soft-delete rows, and a later provider upsert reactivates the same row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.infra.utils.logger import get_logger
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.deals.deal_types import (
    DealResolutionMaps,
    DealWriteResult,
    DimensionHit,
    NormalizedHubSpotDeal,
)

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class _ResolvedFks:
    """Every column the row constructors derive from the resolution maps."""

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


class HubSpotDealWriter:
    """Writer for crm_deals rows from HubSpot deals."""

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
        self.source_mappings = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider="hubspot",
        )
        self._projection = ActivityFeedProjectionService(session, organisation_id)

    async def apply(
        self,
        *,
        normalized: list[NormalizedHubSpotDeal],
        maps: DealResolutionMaps,
        project_deal_created_activity_feed: bool = False,
    ) -> DealWriteResult:
        """Upsert normalized HubSpot deals for this org/integration pair."""
        result = DealWriteResult()
        now = datetime.now(UTC)

        existing_by_external = await self._load_existing_rows()
        deal_created_projection_rows: list[CRMDeal] = []

        for deal in normalized:
            resolved = self._resolve_all(deal, maps, result)

            existing = existing_by_external.get(deal.external_id)
            if existing is None:
                row = self._new_row(deal, resolved, now=now)
                self.session.add(row)
                if project_deal_created_activity_feed:
                    deal_created_projection_rows.append(row)
                await self._upsert_source_mapping(deal, row.deal_id, now=now)
                result.deals_created += 1
            elif _has_same_or_newer_source_modified(existing, deal):
                if project_deal_created_activity_feed:
                    deal_created_projection_rows.append(existing)
                result.deals_skipped_stale += 1
            else:
                # Existing rows include soft-deleted tombstones. Updating the
                # same row preserves local FKs while reactivating restored deals.
                self._apply_updates(existing, deal, resolved, now=now)
                if project_deal_created_activity_feed:
                    deal_created_projection_rows.append(existing)
                await self._upsert_source_mapping(deal, existing.deal_id, now=now)
                result.deals_updated += 1

        await self.session.flush()
        for row in deal_created_projection_rows:
            await self._projection.upsert_deal_created(row)
        logger.info(
            "Deal writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "deals_created": result.deals_created,
                "deals_updated": result.deals_updated,
                "deals_skipped_stale": result.deals_skipped_stale,
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
        deal: NormalizedHubSpotDeal,
        maps: DealResolutionMaps,
        result: DealWriteResult,
    ) -> _ResolvedFks:
        """Resolve every FK / display column against the in-session maps.

        Returns a structured snapshot the row constructors consume — keeps the
        new-row vs update branches symmetrical so they can't drift.
        """
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

        # Pipeline FK can be derived from the stage's owning pipeline as a
        # cross-check / fallback when HubSpot returns a stage but the
        # `pipeline` property is missing or stale.
        if pipeline_hit is None and stage_hit is not None and stage_hit.pipeline_id is not None:
            pipeline_id_from_stage = stage_hit.pipeline_id
        else:
            pipeline_id_from_stage = None

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

    async def _load_existing_rows(self) -> dict[str, CRMDeal]:
        """Every mapped deal row for this integration, keyed by provider id."""
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.DEAL,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.integration_id) == self.integration_id,
            col(CRMDeal.deal_id).in_(set(external_to_local.values())),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        return {
            local_to_external[row.deal_id]: row for row in rows if row.deal_id in local_to_external
        }

    async def _upsert_source_mapping(
        self,
        deal: NormalizedHubSpotDeal,
        local_deal_id: UUID,
        *,
        now: datetime,
    ) -> None:
        await self.source_mappings.upsert_integration_mapping(
            external_object_type=CRMSourceObjectType.DEAL,
            external_id=deal.external_id,
            local_object_type=CRMSourceObjectType.DEAL,
            local_object_id=local_deal_id,
            data_payload=deal.data_payload,
            source_created_at=deal.source_created_at,
            source_modified_at=deal.source_modified_at,
            synced_at=now,
            mapping_metadata=(
                {"associations_payload": deal.associations_payload}
                if deal.associations_payload
                else None
            ),
        )

    def _new_row(
        self,
        deal: NormalizedHubSpotDeal,
        resolved: _ResolvedFks,
        *,
        now: datetime,
    ) -> CRMDeal:
        """Create a CRMDeal row using only resolved local IDs."""
        return CRMDeal(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            name=deal.name,
            amount=_decimal_to_float(deal.amount),
            currency=deal.currency,
            amount_home_currency=_decimal_to_float(deal.amount_home_currency),
            deal_status=deal.deal_status,
            probability=deal.probability,
            predicted_amount=_decimal_to_float(deal.predicted_amount),
            close_date=deal.close_date,
            expected_close_date=deal.expected_close_date,
            closed_won_at=deal.closed_won_at,
            closed_lost_at=deal.closed_lost_at,
            closed_reason=deal.closed_reason,
            crm_url=deal.crm_url,
            stage_name=resolved.stage_name,
            stage_label=resolved.stage_label,
            pipeline_name=resolved.pipeline_name,
            pipeline_label=resolved.pipeline_label,
            pipeline_id=resolved.pipeline_id,
            stage_id=resolved.stage_id,
            forecast_category=deal.forecast_category,
            deal_type=deal.deal_type,
            priority=deal.priority,
            lead_source=deal.lead_source,
            owner_name=resolved.owner_name,
            owner_email=resolved.owner_email,
            owner_id=resolved.owner_id,
            created_by_user_id=resolved.created_by_user_id,
            updated_by_user_id=resolved.updated_by_user_id,
            company_id=resolved.company_id,
            company_name=resolved.company_name,
            company_domain=resolved.company_domain,
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
        )

    def _apply_updates(
        self,
        row: CRMDeal,
        deal: NormalizedHubSpotDeal,
        resolved: _ResolvedFks,
        *,
        now: datetime,
    ) -> None:
        # deal_id PK is referenced by stage history / participants / associations
        # rows — never rewrite. AI-generated `deal_summary_data` is owned by
        # downstream agents; we don't touch it here either.
        row.is_deleted = False
        row.name = deal.name
        row.amount = _decimal_to_float(deal.amount)
        row.currency = deal.currency
        row.amount_home_currency = _decimal_to_float(deal.amount_home_currency)
        row.deal_status = deal.deal_status
        row.probability = deal.probability
        row.predicted_amount = _decimal_to_float(deal.predicted_amount)
        row.close_date = deal.close_date
        row.expected_close_date = deal.expected_close_date
        row.closed_won_at = deal.closed_won_at
        row.closed_lost_at = deal.closed_lost_at
        row.closed_reason = deal.closed_reason
        row.crm_url = deal.crm_url
        row.stage_name = resolved.stage_name
        row.stage_label = resolved.stage_label
        row.pipeline_name = resolved.pipeline_name
        row.pipeline_label = resolved.pipeline_label
        row.pipeline_id = resolved.pipeline_id
        row.stage_id = resolved.stage_id
        row.forecast_category = deal.forecast_category
        row.deal_type = deal.deal_type
        row.priority = deal.priority
        row.lead_source = deal.lead_source
        row.owner_name = resolved.owner_name
        row.owner_email = resolved.owner_email
        row.owner_id = resolved.owner_id
        row.created_by_user_id = resolved.created_by_user_id
        row.updated_by_user_id = resolved.updated_by_user_id
        row.company_id = resolved.company_id
        row.company_name = resolved.company_name
        row.company_domain = resolved.company_domain
        row.primary_contact_id = resolved.primary_contact_id
        row.primary_contact_name = resolved.primary_contact_name
        row.primary_contact_email = resolved.primary_contact_email
        row.source_created_at = deal.source_created_at
        row.last_activity_at = deal.last_activity_at
        row.days_to_close = deal.days_to_close
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


def _lookup(external_id: str | None, table: dict[str, DimensionHit]) -> DimensionHit | None:
    if not external_id:
        return None
    return table.get(external_id)


def _has_same_or_newer_source_modified(
    row: CRMDeal,
    deal: NormalizedHubSpotDeal,
) -> bool:
    """Return True when the active local row already has this provider state."""
    if row.is_deleted or row.source_modified_at is None or deal.source_modified_at is None:
        return False
    return _as_utc(row.source_modified_at) >= _as_utc(deal.source_modified_at)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal_to_float(value: object) -> float | None:
    """CRMDeal columns type these as `float` even though the underlying
    Postgres column is Numeric(18,2). The DB driver round-trips fine with
    floats; converting from Decimal here keeps SQLAlchemy happy.
    """
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]
