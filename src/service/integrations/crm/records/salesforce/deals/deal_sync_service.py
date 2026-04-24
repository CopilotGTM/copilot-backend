"""Salesforce opportunity/deal fact sync orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStage, CRMPipeline
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.backfill import (
    DealFkBackfillOp,
    backfill_crm_deals,
)
from src.service.integrations.crm.records.salesforce.deals.association_writer import (
    SalesforceDealAssociationWriter,
)
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    AssociationWriteResult,
    DealParticipantExtraction,
    DealResolutionMaps,
    DealSyncResult,
    DimensionHit,
    ParticipantWriteResult,
    StageHistoryWriteResult,
)
from src.service.integrations.crm.records.salesforce.deals.deal_writer import (
    SalesforceDealWriter,
)
from src.service.integrations.crm.records.salesforce.deals.opportunity_normalizer import (
    normalize_opportunities,
)
from src.service.integrations.crm.records.salesforce.deals.participant_normalizer import (
    extract_participants,
)
from src.service.integrations.crm.records.salesforce.deals.participant_writer import (
    SalesforceDealParticipantWriter,
)
from src.service.integrations.crm.records.salesforce.deals.stage_history_normalizer import (
    current_stage_entered_at_by_deal,
    normalize_stage_history,
)
from src.service.integrations.crm.records.salesforce.deals.stage_history_writer import (
    SalesforceDealStageHistoryWriter,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.opportunities_fetcher import (
    SalesforceOpportunitiesFetcher,
)
from src.workers.integrations_worker.providers.crm.salesforce.opportunity_contact_roles_fetcher import (
    SalesforceOpportunityContactRolesFetcher,
)
from src.workers.integrations_worker.providers.crm.salesforce.opportunity_history_fetcher import (
    SalesforceOpportunityHistoryFetcher,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class SalesforceDealSyncContext:
    token_info: IntegrationTokenInfo
    instance_url: str


class SalesforceDealSyncConfigurationError(ValueError):
    """Integration not usable for Salesforce deals record sync."""


class SalesforceDealRecordSyncService:
    PROVIDER = "salesforce"
    OBJECT_TYPE = "deal"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> DealSyncResult:
        started_at = datetime.now(UTC)
        context = await self._load_context(organisation_id, integration_id)

        raw_opportunities, field_names = await SalesforceOpportunitiesFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        ).fetch_all()
        raw_stage_history = await SalesforceOpportunityHistoryFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        ).fetch_all()
        raw_contact_roles = await SalesforceOpportunityContactRolesFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        ).fetch_all()

        pipeline_external_id_by_deal = {
            deal_id: (record_type_id or "master")
            for raw in raw_opportunities
            if isinstance(raw, dict)
            for deal_id in [self._coerce_str(raw.get("Id"))]
            if deal_id is not None
            for record_type_id in [self._coerce_str(raw.get("RecordTypeId"))]
        }
        company_external_id_by_deal = {
            deal_id: self._coerce_str(raw.get("AccountId"))
            for raw in raw_opportunities
            if isinstance(raw, dict)
            for deal_id in [self._coerce_str(raw.get("Id"))]
            if deal_id is not None
        }
        current_stage_external_id_by_deal = {
            deal_id: (
                f"{pipeline_external_id_by_deal.get(deal_id, 'master')}:{stage_name}"
                if stage_name is not None
                else None
            )
            for raw in raw_opportunities
            if isinstance(raw, dict)
            for deal_id in [self._coerce_str(raw.get("Id"))]
            if deal_id is not None
            for stage_name in [self._coerce_str(raw.get("StageName"))]
        }

        participant_extraction = extract_participants(
            raw_contact_roles,
            company_external_id_by_deal=company_external_id_by_deal,
        )
        transitions, skipped_malformed_stage_history = normalize_stage_history(
            raw_stage_history,
            pipeline_external_id_by_deal=pipeline_external_id_by_deal,
        )
        stage_entered_at_map = current_stage_entered_at_by_deal(
            transitions,
            current_stage_external_id_by_deal=current_stage_external_id_by_deal,
        )
        normalized_opportunities, skipped_malformed_opportunities = normalize_opportunities(
            raw_opportunities,
            instance_url=context.instance_url,
            primary_contact_by_deal=participant_extraction.primary_contact_by_deal,
            current_stage_entered_at_by_deal=stage_entered_at_map,
            contact_count_by_deal=participant_extraction.contact_count_by_deal,
        )

        (
            write_result,
            stage_history_result,
            participant_result,
            association_result,
            backfill_payload,
        ) = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            opportunities=normalized_opportunities,
            transitions=transitions,
            participant_extraction=participant_extraction,
        )

        completed_at = datetime.now(UTC)
        return DealSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_opportunities=len(raw_opportunities),
            property_list_size=len(field_names),
            fetched_stage_history_rows=len(raw_stage_history),
            fetched_participant_rows=len(raw_contact_roles),
            deals_created=write_result.deals_created,
            deals_updated=write_result.deals_updated,
            unresolved_pipeline_link=write_result.unresolved_pipeline_link,
            unresolved_stage_link=write_result.unresolved_stage_link,
            unresolved_owner_link=write_result.unresolved_owner_link,
            unresolved_company_link=write_result.unresolved_company_link,
            unresolved_contact_link=write_result.unresolved_contact_link,
            unresolved_created_by_link=write_result.unresolved_created_by_link,
            unresolved_updated_by_link=write_result.unresolved_updated_by_link,
            skipped_malformed_opportunities=skipped_malformed_opportunities,
            skipped_malformed_stage_history=skipped_malformed_stage_history,
            skipped_malformed_participants=participant_extraction.skipped_malformed,
            stage_history_created=stage_history_result.stage_history_created,
            stage_history_updated=stage_history_result.stage_history_updated,
            stage_history_unresolved_stage=stage_history_result.unresolved_stage,
            stage_history_unresolved_from_stage=stage_history_result.unresolved_from_stage,
            participants_created=participant_result.participants_created,
            participants_updated=participant_result.participants_updated,
            participants_unresolved_related=participant_result.unresolved_related,
            associations_created=association_result.associations_created,
            associations_updated=association_result.associations_updated,
            associations_unresolved_to_local=association_result.unresolved_to_local,
            deal_backfill_rows_updated=backfill_payload["rows_updated"],
            deal_backfill_per_op=backfill_payload["per_op"],
        )

    async def _load_context(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SalesforceDealSyncContext:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise SalesforceDealSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.SALESFORCE:
                raise SalesforceDealSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected salesforce"
                )
            if not integration.is_active:
                raise SalesforceDealSyncConfigurationError(
                    f"Integration {integration_id} is not active"
                )
            credentials = dict(integration.credentials or {})
            instance_url = str(credentials.get("instance_url") or "").rstrip("/")
            if not instance_url:
                raise SalesforceDealSyncConfigurationError(
                    f"Integration {integration_id} is missing Salesforce instance_url"
                )
            return SalesforceDealSyncContext(
                token_info=IntegrationTokenInfo.from_integration(integration, organisation_id),
                instance_url=instance_url,
            )
        raise SalesforceDealSyncConfigurationError(
            "Could not acquire org session for Salesforce deals record sync"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        opportunities: list[Any],
        transitions: list[Any],
        participant_extraction: DealParticipantExtraction,
    ) -> tuple[
        Any, StageHistoryWriteResult, ParticipantWriteResult, AssociationWriteResult, dict[str, Any]
    ]:
        async for session in acquire_org_async_session(organisation_id):
            maps = await self._load_resolution_maps(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            write_result = await SalesforceDealWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            ).apply(
                normalized=opportunities,
                maps=maps,
            )

            deal_pk_map = await self._load_deal_pk_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            stage_history_result = await SalesforceDealStageHistoryWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            ).apply(
                transitions=transitions,
                deal_pk_map=deal_pk_map,
                stage_map=maps.stage_map,
            )

            participant_result = await SalesforceDealParticipantWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            ).apply(
                participants=participant_extraction.participants,
                deal_pk_map=deal_pk_map,
                contact_map=maps.contact_map,
                company_map=maps.company_map,
                user_map=maps.user_map,
            )

            association_result = await SalesforceDealAssociationWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            ).apply(
                edges=participant_extraction.associations,
                deal_pk_map=deal_pk_map,
                contact_map=maps.contact_map,
                company_map=maps.company_map,
            )

            backfill_payload = await self._backfill_deals(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                maps=maps,
            )

            await session.commit()
            return (
                write_result,
                stage_history_result,
                participant_result,
                association_result,
                backfill_payload,
            )

        raise SalesforceDealSyncConfigurationError(
            "Could not acquire org session for Salesforce deals record sync write"
        )

    async def _backfill_deals(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        maps: DealResolutionMaps,
    ) -> dict[str, Any]:
        owner_mapping = {
            external_id: (hit.internal_id, hit.name, None, hit.email)
            for external_id, hit in maps.user_map.items()
        }
        fk_only_mapping = {
            external_id: (hit.internal_id, None, None, None)
            for external_id, hit in maps.user_map.items()
        }
        company_mapping = {
            external_id: (hit.internal_id, hit.name, hit.domain, None)
            for external_id, hit in maps.company_map.items()
        }
        contact_mapping = {
            external_id: (hit.internal_id, hit.name, None, hit.email)
            for external_id, hit in maps.contact_map.items()
        }
        ops: list[DealFkBackfillOp] = []
        if owner_mapping:
            ops.append(
                DealFkBackfillOp(
                    external_col="owner_external_id",
                    fk_col="owner_id",
                    name_col="owner_name",
                    email_col="owner_email",
                    mapping=owner_mapping,
                )
            )
            ops.append(
                DealFkBackfillOp(
                    external_col="created_by_user_external_id",
                    fk_col="created_by_user_id",
                    mapping=fk_only_mapping,
                )
            )
            ops.append(
                DealFkBackfillOp(
                    external_col="updated_by_user_external_id",
                    fk_col="updated_by_user_id",
                    mapping=fk_only_mapping,
                )
            )
        if company_mapping:
            ops.append(
                DealFkBackfillOp(
                    external_col="company_external_id",
                    fk_col="company_id",
                    name_col="company_name",
                    label_col="company_domain",
                    mapping=company_mapping,
                )
            )
        if contact_mapping:
            ops.append(
                DealFkBackfillOp(
                    external_col="primary_contact_external_id",
                    fk_col="primary_contact_id",
                    name_col="primary_contact_name",
                    email_col="primary_contact_email",
                    mapping=contact_mapping,
                )
            )
        if not ops:
            return {"rows_updated": 0, "per_op": {}}
        backfill_result = await backfill_crm_deals(
            session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            ops=ops,
        )
        return {
            "rows_updated": backfill_result.rows_updated,
            "per_op": dict(backfill_result.per_op),
        }

    async def _load_deal_pk_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {row.external_id: row.deal_id for row in rows if row.external_id is not None}

    async def _load_resolution_maps(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> DealResolutionMaps:
        pipeline_map: dict[str, DimensionHit] = {}
        stage_map: dict[str, DimensionHit] = {}
        user_map: dict[str, DimensionHit] = {}
        company_map: dict[str, DimensionHit] = {}
        contact_map: dict[str, DimensionHit] = {}

        pipeline_stmt = select(CRMPipeline).where(
            col(CRMPipeline.organisation_id) == organisation_id,
            col(CRMPipeline.integration_id) == integration_id,
            col(CRMPipeline.is_deleted) == False,  # noqa: E712
        )
        for pipeline in (await session.execute(pipeline_stmt)).scalars().all():
            if pipeline.external_id:
                pipeline_map[pipeline.external_id] = DimensionHit(
                    internal_id=pipeline.pipeline_id,
                    name=pipeline.name,
                    label=pipeline.label,
                )

        stage_stmt = select(CRMDealStage).where(
            col(CRMDealStage.organisation_id) == organisation_id,
            col(CRMDealStage.integration_id) == integration_id,
            col(CRMDealStage.is_deleted) == False,  # noqa: E712
        )
        for stage in (await session.execute(stage_stmt)).scalars().all():
            if stage.external_id:
                stage_map[stage.external_id] = DimensionHit(
                    internal_id=stage.stage_id,
                    name=stage.name,
                    label=stage.label,
                    pipeline_id=stage.pipeline_id,
                )

        user_stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
        )
        for user in (await session.execute(user_stmt)).scalars().all():
            if user.external_id:
                user_map[user.external_id] = DimensionHit(
                    internal_id=user.user_id,
                    name=user.full_name,
                    email=user.email,
                )

        company_stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        for company in (await session.execute(company_stmt)).scalars().all():
            if company.external_id:
                company_map[company.external_id] = DimensionHit(
                    internal_id=company.company_id,
                    name=company.name,
                    domain=company.domain,
                )

        contact_stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        for contact in (await session.execute(contact_stmt)).scalars().all():
            if contact.external_id:
                contact_map[contact.external_id] = DimensionHit(
                    internal_id=contact.contact_id,
                    name=contact.full_name,
                    email=contact.email,
                )

        return DealResolutionMaps(
            pipeline_map=pipeline_map,
            stage_map=stage_map,
            user_map=user_map,
            company_map=company_map,
            contact_map=contact_map,
        )

    @staticmethod
    def _coerce_str(value: Any) -> str | None:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return None
