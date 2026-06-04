"""Salesforce opportunity/deal fact sync orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm.associations import CRMObjectType
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMParticipantType,
    CRMPipeline,
)
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
    DealWriteResult,
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

_DEAL_PERSIST_CHUNK_SIZE = 2_000
_DEAL_RELATIONSHIP_CHUNK_SIZE = 2_000


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

        opportunities_fetcher = SalesforceOpportunitiesFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        )
        pending_raw: list[dict[str, Any]] = []
        fetched_opportunities = 0
        property_list_size = 0
        skipped_malformed_opportunities = 0
        pipeline_external_id_by_deal: dict[str, str] = {}
        company_external_id_by_deal: dict[str, str | None] = {}
        current_stage_external_id_by_deal: dict[str, str | None] = {}
        write_result = DealWriteResult()
        stage_history_result = StageHistoryWriteResult()
        participant_result = ParticipantWriteResult()
        association_result = AssociationWriteResult()
        backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
        fetched_stage_history_rows = 0
        fetched_participant_rows = 0

        async for page_records, field_names in opportunities_fetcher.fetch_pages():
            fetched_opportunities += len(page_records)
            if field_names:
                property_list_size = len(field_names)
            self._capture_opportunity_dimensions(
                raw_opportunities=page_records,
                pipeline_external_id_by_deal=pipeline_external_id_by_deal,
                company_external_id_by_deal=company_external_id_by_deal,
                current_stage_external_id_by_deal=current_stage_external_id_by_deal,
            )
            pending_raw.extend(page_records)
            if len(pending_raw) < _DEAL_PERSIST_CHUNK_SIZE:
                continue
            chunk_result = await self._persist_opportunity_chunk(
                organisation_id=organisation_id,
                integration_id=integration_id,
                instance_url=context.instance_url,
                raw_opportunities=pending_raw,
            )
            self._merge_deal_write_result(write_result, chunk_result[0])
            skipped_malformed_opportunities += chunk_result[1]
            pending_raw = []

        if pending_raw:
            chunk_result = await self._persist_opportunity_chunk(
                organisation_id=organisation_id,
                integration_id=integration_id,
                instance_url=context.instance_url,
                raw_opportunities=pending_raw,
            )
            self._merge_deal_write_result(write_result, chunk_result[0])
            skipped_malformed_opportunities += chunk_result[1]

        (
            fetched_stage_history_rows,
            skipped_malformed_stage_history,
            stage_history_result,
            stage_backfill_payload,
        ) = await self._sync_stage_history_relationships(
            organisation_id=organisation_id,
            integration_id=integration_id,
            token_info=context.token_info,
            instance_url=context.instance_url,
            pipeline_external_id_by_deal=pipeline_external_id_by_deal,
            current_stage_external_id_by_deal=current_stage_external_id_by_deal,
        )
        self._merge_backfill_payload(backfill_payload, stage_backfill_payload)

        (
            fetched_participant_rows,
            skipped_malformed_participants,
            role_participant_result,
            role_association_result,
            role_backfill_payload,
        ) = await self._sync_contact_role_relationships(
            organisation_id=organisation_id,
            integration_id=integration_id,
            token_info=context.token_info,
            instance_url=context.instance_url,
        )
        self._merge_participant_result(participant_result, role_participant_result)
        self._merge_association_result(association_result, role_association_result)
        self._merge_backfill_payload(backfill_payload, role_backfill_payload)

        (
            company_participant_result,
            company_association_result,
            company_backfill_payload,
        ) = await self._sync_company_relationships(
            organisation_id=organisation_id,
            integration_id=integration_id,
            company_external_id_by_deal=company_external_id_by_deal,
        )
        self._merge_participant_result(participant_result, company_participant_result)
        self._merge_association_result(association_result, company_association_result)
        self._merge_backfill_payload(backfill_payload, company_backfill_payload)

        completed_at = datetime.now(UTC)
        return DealSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_opportunities=fetched_opportunities,
            property_list_size=property_list_size,
            fetched_stage_history_rows=fetched_stage_history_rows,
            fetched_participant_rows=fetched_participant_rows,
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
            skipped_malformed_participants=skipped_malformed_participants,
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

    async def _persist_opportunity_chunk(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        instance_url: str,
        raw_opportunities: list[dict[str, Any]],
    ) -> tuple[DealWriteResult, int]:
        normalized_opportunities, skipped_malformed_opportunities = normalize_opportunities(
            raw_opportunities,
            instance_url=instance_url,
            primary_contact_by_deal={},
            current_stage_entered_at_by_deal={},
            contact_count_by_deal={},
        )
        logger.info(
            "Salesforce deals chunk persist started",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "raw_opportunities": len(raw_opportunities),
                "normalized_opportunities": len(normalized_opportunities),
            },
        )
        write_result, *_ = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            opportunities=normalized_opportunities,
            transitions=[],
            participant_extraction=DealParticipantExtraction(),
            backfill=False,
            preserve_relationship_fields=True,
        )
        logger.info(
            "Salesforce deals chunk persist completed",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "deals_created": write_result.deals_created,
                "deals_updated": write_result.deals_updated,
                "skipped_malformed_opportunities": skipped_malformed_opportunities,
            },
        )
        return write_result, skipped_malformed_opportunities

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        opportunities: list[Any],
        transitions: list[Any],
        participant_extraction: DealParticipantExtraction,
        backfill: bool = True,
        preserve_relationship_fields: bool = False,
    ) -> tuple[
        Any, StageHistoryWriteResult, ParticipantWriteResult, AssociationWriteResult, dict[str, Any]
    ]:
        async for session in acquire_org_async_session(organisation_id):
            resolution_filters = self._resolution_filters(
                opportunities=opportunities,
                transitions=transitions,
                participant_extraction=participant_extraction,
            )
            maps = await self._load_resolution_maps(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                pipeline_external_ids=resolution_filters["pipeline_external_ids"],
                stage_external_ids=resolution_filters["stage_external_ids"],
                user_external_ids=resolution_filters["user_external_ids"],
                company_external_ids=resolution_filters["company_external_ids"],
                contact_external_ids=resolution_filters["contact_external_ids"],
            )

            write_result = await SalesforceDealWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            ).apply(
                normalized=opportunities,
                maps=maps,
                preserve_relationship_fields=preserve_relationship_fields,
            )

            stage_history_result = StageHistoryWriteResult()
            participant_result = ParticipantWriteResult()
            association_result = AssociationWriteResult()
            has_relationship_rows = bool(
                transitions
                or participant_extraction.participants
                or participant_extraction.associations
            )
            if has_relationship_rows:
                deal_pk_map = await self._load_deal_pk_map(
                    session=session,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    external_ids=self._relationship_deal_external_ids(
                        transitions=transitions,
                        participant_extraction=participant_extraction,
                    ),
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

            backfill_payload = (
                await self._backfill_deals(
                    session=session,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    maps=maps,
                )
                if backfill
                else {"rows_updated": 0, "per_op": {}}
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

    async def _update_deal_relationship_snapshots(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        stage_entered_at_by_deal: dict[str, datetime],
        primary_contact_by_deal: dict[str, str],
        contact_count_by_deal: dict[str, int],
    ) -> None:
        external_ids = (
            set(stage_entered_at_by_deal)
            | set(primary_contact_by_deal)
            | set(contact_count_by_deal)
        )
        if not external_ids:
            return

        async for session in acquire_org_async_session(organisation_id):
            stmt = select(CRMDeal).where(
                col(CRMDeal.organisation_id) == organisation_id,
                col(CRMDeal.integration_id) == integration_id,
                col(CRMDeal.is_deleted) == False,  # noqa: E712
                col(CRMDeal.external_id).in_(external_ids),
            )
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                external_id = row.external_id
                if external_id is None:
                    continue
                if external_id in stage_entered_at_by_deal:
                    row.stage_entered_at = stage_entered_at_by_deal[external_id]
                if external_id in primary_contact_by_deal:
                    row.primary_contact_external_id = primary_contact_by_deal[external_id]
                if external_id in contact_count_by_deal:
                    row.associated_contact_count = contact_count_by_deal[external_id]
            await session.commit()
            logger.info(
                "Salesforce deal relationship snapshots updated",
                extra={
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "deals_updated": len(rows),
                },
            )
            return

        raise SalesforceDealSyncConfigurationError(
            "Could not acquire org session for Salesforce deal relationship snapshot update"
        )

    async def _sync_stage_history_relationships(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        token_info: IntegrationTokenInfo,
        instance_url: str,
        pipeline_external_id_by_deal: dict[str, str],
        current_stage_external_id_by_deal: dict[str, str | None],
    ) -> tuple[int, int, StageHistoryWriteResult, dict[str, Any]]:
        fetched_rows = 0
        skipped_malformed = 0
        result = StageHistoryWriteResult()
        backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
        carry: list[dict[str, Any]] = []

        async for page_records in SalesforceOpportunityHistoryFetcher(
            token_info=token_info,
            instance_url=instance_url,
        ).fetch_pages():
            fetched_rows += len(page_records)
            completed_rows, carry = self._split_ordered_rows_by_opportunity(carry + page_records)
            if not completed_rows:
                continue
            chunk_result, chunk_skipped, chunk_backfill = await self._persist_stage_history_rows(
                organisation_id=organisation_id,
                integration_id=integration_id,
                raw_stage_history=completed_rows,
                pipeline_external_id_by_deal=pipeline_external_id_by_deal,
                current_stage_external_id_by_deal=current_stage_external_id_by_deal,
            )
            self._merge_stage_history_result(result, chunk_result)
            skipped_malformed += chunk_skipped
            self._merge_backfill_payload(backfill_payload, chunk_backfill)

        if carry:
            chunk_result, chunk_skipped, chunk_backfill = await self._persist_stage_history_rows(
                organisation_id=organisation_id,
                integration_id=integration_id,
                raw_stage_history=carry,
                pipeline_external_id_by_deal=pipeline_external_id_by_deal,
                current_stage_external_id_by_deal=current_stage_external_id_by_deal,
            )
            self._merge_stage_history_result(result, chunk_result)
            skipped_malformed += chunk_skipped
            self._merge_backfill_payload(backfill_payload, chunk_backfill)

        return fetched_rows, skipped_malformed, result, backfill_payload

    async def _persist_stage_history_rows(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_stage_history: list[dict[str, Any]],
        pipeline_external_id_by_deal: dict[str, str],
        current_stage_external_id_by_deal: dict[str, str | None],
    ) -> tuple[StageHistoryWriteResult, int, dict[str, Any]]:
        transitions, skipped_malformed = normalize_stage_history(
            raw_stage_history,
            pipeline_external_id_by_deal=pipeline_external_id_by_deal,
        )
        if not transitions:
            return StageHistoryWriteResult(), skipped_malformed, {"rows_updated": 0, "per_op": {}}

        stage_entered_at_map = current_stage_entered_at_by_deal(
            transitions,
            current_stage_external_id_by_deal=current_stage_external_id_by_deal,
        )
        await self._update_deal_relationship_snapshots(
            organisation_id=organisation_id,
            integration_id=integration_id,
            stage_entered_at_by_deal=stage_entered_at_map,
            primary_contact_by_deal={},
            contact_count_by_deal={},
        )
        _, stage_history_result, _, _, backfill_payload = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            opportunities=[],
            transitions=transitions,
            participant_extraction=DealParticipantExtraction(),
        )
        return stage_history_result, skipped_malformed, backfill_payload

    async def _sync_contact_role_relationships(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> tuple[int, int, ParticipantWriteResult, AssociationWriteResult, dict[str, Any]]:
        fetched_rows = 0
        skipped_malformed = 0
        participant_result = ParticipantWriteResult()
        association_result = AssociationWriteResult()
        backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
        carry: list[dict[str, Any]] = []

        async for page_records in SalesforceOpportunityContactRolesFetcher(
            token_info=token_info,
            instance_url=instance_url,
        ).fetch_pages():
            fetched_rows += len(page_records)
            completed_rows, carry = self._split_ordered_rows_by_opportunity(carry + page_records)
            if not completed_rows:
                continue
            chunk_result = await self._persist_contact_role_rows(
                organisation_id=organisation_id,
                integration_id=integration_id,
                raw_contact_roles=completed_rows,
            )
            skipped_malformed += chunk_result[0]
            self._merge_participant_result(participant_result, chunk_result[1])
            self._merge_association_result(association_result, chunk_result[2])
            self._merge_backfill_payload(backfill_payload, chunk_result[3])

        if carry:
            chunk_result = await self._persist_contact_role_rows(
                organisation_id=organisation_id,
                integration_id=integration_id,
                raw_contact_roles=carry,
            )
            skipped_malformed += chunk_result[0]
            self._merge_participant_result(participant_result, chunk_result[1])
            self._merge_association_result(association_result, chunk_result[2])
            self._merge_backfill_payload(backfill_payload, chunk_result[3])

        return (
            fetched_rows,
            skipped_malformed,
            participant_result,
            association_result,
            backfill_payload,
        )

    async def _persist_contact_role_rows(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_contact_roles: list[dict[str, Any]],
    ) -> tuple[int, ParticipantWriteResult, AssociationWriteResult, dict[str, Any]]:
        participant_extraction = extract_participants(
            raw_contact_roles,
            company_external_id_by_deal={},
        )
        await self._update_deal_relationship_snapshots(
            organisation_id=organisation_id,
            integration_id=integration_id,
            stage_entered_at_by_deal={},
            primary_contact_by_deal=participant_extraction.primary_contact_by_deal,
            contact_count_by_deal=participant_extraction.contact_count_by_deal,
        )
        if not participant_extraction.participants and not participant_extraction.associations:
            return (
                participant_extraction.skipped_malformed,
                ParticipantWriteResult(),
                AssociationWriteResult(),
                {"rows_updated": 0, "per_op": {}},
            )

        _, _, participant_result, association_result, backfill_payload = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            opportunities=[],
            transitions=[],
            participant_extraction=participant_extraction,
        )
        return (
            participant_extraction.skipped_malformed,
            participant_result,
            association_result,
            backfill_payload,
        )

    async def _sync_company_relationships(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        company_external_id_by_deal: dict[str, str | None],
    ) -> tuple[ParticipantWriteResult, AssociationWriteResult, dict[str, Any]]:
        participant_result = ParticipantWriteResult()
        association_result = AssociationWriteResult()
        backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}

        items = list(company_external_id_by_deal.items())
        for start in range(0, len(items), _DEAL_RELATIONSHIP_CHUNK_SIZE):
            chunk_map = dict(items[start : start + _DEAL_RELATIONSHIP_CHUNK_SIZE])
            participant_extraction = extract_participants(
                [],
                company_external_id_by_deal=chunk_map,
            )
            if not participant_extraction.participants and not participant_extraction.associations:
                continue
            _, _, chunk_participant, chunk_association, chunk_backfill = await self._persist(
                organisation_id=organisation_id,
                integration_id=integration_id,
                opportunities=[],
                transitions=[],
                participant_extraction=participant_extraction,
            )
            self._merge_participant_result(participant_result, chunk_participant)
            self._merge_association_result(association_result, chunk_association)
            self._merge_backfill_payload(backfill_payload, chunk_backfill)

        return participant_result, association_result, backfill_payload

    async def persist_single_opportunity(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_opportunity: dict[str, Any],
        instance_url: str,
    ) -> dict[str, Any]:
        normalized_opportunities, skipped_malformed_opportunities = normalize_opportunities(
            [raw_opportunity],
            instance_url=instance_url,
            primary_contact_by_deal={},
            current_stage_entered_at_by_deal={},
            contact_count_by_deal={},
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
            transitions=[],
            participant_extraction=DealParticipantExtraction(),
        )
        return {
            "fetched_opportunities": 1,
            "deals_created": write_result.deals_created,
            "deals_updated": write_result.deals_updated,
            "unresolved_pipeline_link": write_result.unresolved_pipeline_link,
            "unresolved_stage_link": write_result.unresolved_stage_link,
            "unresolved_owner_link": write_result.unresolved_owner_link,
            "unresolved_company_link": write_result.unresolved_company_link,
            "unresolved_contact_link": write_result.unresolved_contact_link,
            "unresolved_created_by_link": write_result.unresolved_created_by_link,
            "unresolved_updated_by_link": write_result.unresolved_updated_by_link,
            "skipped_malformed_opportunities": skipped_malformed_opportunities,
            "stage_history_created": stage_history_result.stage_history_created,
            "stage_history_updated": stage_history_result.stage_history_updated,
            "participants_created": participant_result.participants_created,
            "participants_updated": participant_result.participants_updated,
            "associations_created": association_result.associations_created,
            "associations_updated": association_result.associations_updated,
            "deal_backfill_rows_updated": backfill_payload["rows_updated"],
            "deal_backfill_per_op": backfill_payload["per_op"],
        }

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
        external_ids: set[str] | None = None,
    ) -> dict[str, UUID]:
        stmt = select(CRMDeal.external_id, CRMDeal.deal_id).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
        )
        if external_ids is not None:
            if not external_ids:
                return {}
            stmt = stmt.where(col(CRMDeal.external_id).in_(external_ids))
        rows = (await session.execute(stmt)).all()
        return {external_id: deal_id for external_id, deal_id in rows if external_id is not None}

    async def _load_resolution_maps(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        pipeline_external_ids: set[str],
        stage_external_ids: set[str],
        user_external_ids: set[str],
        company_external_ids: set[str],
        contact_external_ids: set[str],
    ) -> DealResolutionMaps:
        pipeline_map: dict[str, DimensionHit] = {}
        stage_map: dict[str, DimensionHit] = {}
        user_map: dict[str, DimensionHit] = {}
        company_map: dict[str, DimensionHit] = {}
        contact_map: dict[str, DimensionHit] = {}

        if pipeline_external_ids:
            pipeline_stmt = select(
                CRMPipeline.external_id,
                CRMPipeline.pipeline_id,
                CRMPipeline.name,
                CRMPipeline.label,
            ).where(
                col(CRMPipeline.organisation_id) == organisation_id,
                col(CRMPipeline.integration_id) == integration_id,
                col(CRMPipeline.is_deleted) == False,  # noqa: E712
                col(CRMPipeline.external_id).in_(pipeline_external_ids),
            )
            for external_id, pipeline_id, name, label in (
                await session.execute(pipeline_stmt)
            ).all():
                if external_id:
                    pipeline_map[external_id] = DimensionHit(
                        internal_id=pipeline_id,
                        name=name,
                        label=label,
                    )

        if stage_external_ids:
            stage_stmt = select(
                CRMDealStage.external_id,
                CRMDealStage.stage_id,
                CRMDealStage.name,
                CRMDealStage.label,
                CRMDealStage.pipeline_id,
            ).where(
                col(CRMDealStage.organisation_id) == organisation_id,
                col(CRMDealStage.integration_id) == integration_id,
                col(CRMDealStage.is_deleted) == False,  # noqa: E712
                col(CRMDealStage.external_id).in_(stage_external_ids),
            )
            for external_id, stage_id, name, label, pipeline_id in (
                await session.execute(stage_stmt)
            ).all():
                if external_id:
                    stage_map[external_id] = DimensionHit(
                        internal_id=stage_id,
                        name=name,
                        label=label,
                        pipeline_id=pipeline_id,
                    )

        if user_external_ids:
            user_stmt = select(
                CRMUser.external_id,
                CRMUser.user_id,
                CRMUser.full_name,
                CRMUser.email,
            ).where(
                col(CRMUser.organisation_id) == organisation_id,
                col(CRMUser.integration_id) == integration_id,
                col(CRMUser.is_deleted) == False,  # noqa: E712
                col(CRMUser.external_id).in_(user_external_ids),
            )
            for external_id, user_id, full_name, email in (await session.execute(user_stmt)).all():
                if external_id:
                    user_map[external_id] = DimensionHit(
                        internal_id=user_id,
                        name=full_name,
                        email=email,
                    )

        if company_external_ids:
            company_stmt = select(
                CRMCompany.external_id,
                CRMCompany.company_id,
                CRMCompany.name,
                CRMCompany.domain,
            ).where(
                col(CRMCompany.organisation_id) == organisation_id,
                col(CRMCompany.integration_id) == integration_id,
                col(CRMCompany.is_deleted) == False,  # noqa: E712
                col(CRMCompany.external_id).in_(company_external_ids),
            )
            for external_id, company_id, name, domain in (
                await session.execute(company_stmt)
            ).all():
                if external_id:
                    company_map[external_id] = DimensionHit(
                        internal_id=company_id,
                        name=name,
                        domain=domain,
                    )

        if contact_external_ids:
            contact_stmt = select(
                CRMContact.external_id,
                CRMContact.contact_id,
                CRMContact.full_name,
                CRMContact.email,
            ).where(
                col(CRMContact.organisation_id) == organisation_id,
                col(CRMContact.integration_id) == integration_id,
                col(CRMContact.is_deleted) == False,  # noqa: E712
                col(CRMContact.external_id).in_(contact_external_ids),
            )
            for external_id, contact_id, full_name, email in (
                await session.execute(contact_stmt)
            ).all():
                if external_id:
                    contact_map[external_id] = DimensionHit(
                        internal_id=contact_id,
                        name=full_name,
                        email=email,
                    )

        logger.info(
            "Loaded deal resolution maps",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "pipelines": len(pipeline_map),
                "stages": len(stage_map),
                "users": len(user_map),
                "companies": len(company_map),
                "contacts": len(contact_map),
                "requested_pipelines": len(pipeline_external_ids),
                "requested_stages": len(stage_external_ids),
                "requested_users": len(user_external_ids),
                "requested_companies": len(company_external_ids),
                "requested_contacts": len(contact_external_ids),
            },
        )

        return DealResolutionMaps(
            pipeline_map=pipeline_map,
            stage_map=stage_map,
            user_map=user_map,
            company_map=company_map,
            contact_map=contact_map,
        )

    @staticmethod
    def _resolution_filters(
        *,
        opportunities: list[Any],
        transitions: list[Any],
        participant_extraction: DealParticipantExtraction,
    ) -> dict[str, set[str]]:
        pipeline_external_ids = _compact_str_set(
            opportunity.pipeline_external_id for opportunity in opportunities
        )
        stage_external_ids = _compact_str_set(
            opportunity.stage_external_id for opportunity in opportunities
        )
        stage_external_ids.update(
            _compact_str_set(transition.stage_external_id for transition in transitions)
        )
        stage_external_ids.update(
            _compact_str_set(transition.from_stage_external_id for transition in transitions)
        )
        user_external_ids = _compact_str_set(
            user_id
            for opportunity in opportunities
            for user_id in (
                opportunity.owner_external_id,
                opportunity.created_by_user_external_id,
                opportunity.updated_by_user_external_id,
            )
        )
        company_external_ids = _compact_str_set(
            opportunity.primary_company_external_id for opportunity in opportunities
        )
        contact_external_ids = _compact_str_set(
            opportunity.primary_contact_external_id for opportunity in opportunities
        )

        for participant in participant_extraction.participants:
            if participant.participant_type == CRMParticipantType.CONTACT.value:
                contact_external_ids.add(participant.related_external_id)
            elif participant.participant_type == CRMParticipantType.COMPANY.value:
                company_external_ids.add(participant.related_external_id)
            elif participant.participant_type == CRMParticipantType.USER.value:
                user_external_ids.add(participant.related_external_id)

        for association in participant_extraction.associations:
            if association.to_object_type == CRMObjectType.CONTACT.value:
                contact_external_ids.add(association.to_external_id)
            elif association.to_object_type == CRMObjectType.COMPANY.value:
                company_external_ids.add(association.to_external_id)

        return {
            "pipeline_external_ids": pipeline_external_ids,
            "stage_external_ids": stage_external_ids,
            "user_external_ids": user_external_ids,
            "company_external_ids": company_external_ids,
            "contact_external_ids": contact_external_ids,
        }

    def _capture_opportunity_dimensions(
        self,
        *,
        raw_opportunities: list[dict[str, Any]],
        pipeline_external_id_by_deal: dict[str, str],
        company_external_id_by_deal: dict[str, str | None],
        current_stage_external_id_by_deal: dict[str, str | None],
    ) -> None:
        for raw in raw_opportunities:
            deal_id = self._coerce_str(raw.get("Id"))
            if deal_id is None:
                continue
            record_type_id = self._coerce_str(raw.get("RecordTypeId"))
            pipeline_external_id = record_type_id or "master"
            pipeline_external_id_by_deal[deal_id] = pipeline_external_id
            company_external_id_by_deal[deal_id] = self._coerce_str(raw.get("AccountId"))
            stage_name = self._coerce_str(raw.get("StageName"))
            current_stage_external_id_by_deal[deal_id] = (
                f"{pipeline_external_id}:{stage_name}" if stage_name is not None else None
            )

    @staticmethod
    def _merge_deal_write_result(target: DealWriteResult, source: DealWriteResult) -> None:
        target.deals_created += source.deals_created
        target.deals_updated += source.deals_updated
        target.unresolved_pipeline_link += source.unresolved_pipeline_link
        target.unresolved_stage_link += source.unresolved_stage_link
        target.unresolved_owner_link += source.unresolved_owner_link
        target.unresolved_company_link += source.unresolved_company_link
        target.unresolved_contact_link += source.unresolved_contact_link
        target.unresolved_created_by_link += source.unresolved_created_by_link
        target.unresolved_updated_by_link += source.unresolved_updated_by_link

    @staticmethod
    def _merge_stage_history_result(
        target: StageHistoryWriteResult,
        source: StageHistoryWriteResult,
    ) -> None:
        target.stage_history_created += source.stage_history_created
        target.stage_history_updated += source.stage_history_updated
        target.unresolved_stage += source.unresolved_stage
        target.unresolved_from_stage += source.unresolved_from_stage

    @staticmethod
    def _merge_participant_result(
        target: ParticipantWriteResult,
        source: ParticipantWriteResult,
    ) -> None:
        target.participants_created += source.participants_created
        target.participants_updated += source.participants_updated
        target.unresolved_related += source.unresolved_related

    @staticmethod
    def _merge_association_result(
        target: AssociationWriteResult,
        source: AssociationWriteResult,
    ) -> None:
        target.associations_created += source.associations_created
        target.associations_updated += source.associations_updated
        target.unresolved_to_local += source.unresolved_to_local

    @staticmethod
    def _merge_backfill_payload(target: dict[str, Any], source: dict[str, Any]) -> None:
        target["rows_updated"] += source["rows_updated"]
        per_op = target["per_op"]
        for op_name, rows_updated in source["per_op"].items():
            per_op[op_name] = per_op.get(op_name, 0) + rows_updated

    @staticmethod
    def _relationship_deal_external_ids(
        *,
        transitions: list[Any],
        participant_extraction: DealParticipantExtraction,
    ) -> set[str]:
        return {
            external_id
            for external_id in (
                [transition.deal_external_id for transition in transitions]
                + [
                    participant.deal_external_id
                    for participant in participant_extraction.participants
                ]
                + [
                    association.from_external_id
                    for association in participant_extraction.associations
                ]
            )
            if isinstance(external_id, str) and external_id
        }

    @classmethod
    def _split_ordered_rows_by_opportunity(
        cls,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not rows:
            return [], []
        last_opportunity_id = cls._coerce_str(rows[-1].get("OpportunityId"))
        if last_opportunity_id is None:
            return rows, []

        completed: list[dict[str, Any]] = []
        carry: list[dict[str, Any]] = []
        for row in rows:
            if cls._coerce_str(row.get("OpportunityId")) == last_opportunity_id:
                carry.append(row)
            else:
                completed.append(row)
        return completed, carry

    @staticmethod
    def _coerce_str(value: Any) -> str | None:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return None


def _compact_str_set(values) -> set[str]:
    return {value for value in values if isinstance(value, str) and value}
