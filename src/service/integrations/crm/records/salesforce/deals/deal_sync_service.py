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

        raw_stage_history: list[dict[str, Any]] = []
        async for page_records in SalesforceOpportunityHistoryFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        ).fetch_pages():
            raw_stage_history.extend(page_records)
        transitions, skipped_malformed_stage_history = normalize_stage_history(
            raw_stage_history,
            pipeline_external_id_by_deal=pipeline_external_id_by_deal,
        )
        stage_entered_at_map = current_stage_entered_at_by_deal(
            transitions,
            current_stage_external_id_by_deal=current_stage_external_id_by_deal,
        )

        raw_contact_roles: list[dict[str, Any]] = []
        async for page_records in SalesforceOpportunityContactRolesFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        ).fetch_pages():
            raw_contact_roles.extend(page_records)
        participant_extraction = extract_participants(
            raw_contact_roles,
            company_external_id_by_deal=company_external_id_by_deal,
        )
        await self._update_deal_relationship_snapshots(
            organisation_id=organisation_id,
            integration_id=integration_id,
            stage_entered_at_by_deal=stage_entered_at_map,
            primary_contact_by_deal=participant_extraction.primary_contact_by_deal,
            contact_count_by_deal=participant_extraction.contact_count_by_deal,
        )

        if (
            transitions
            or participant_extraction.participants
            or participant_extraction.associations
        ):
            (
                side_write_result,
                stage_history_result,
                participant_result,
                association_result,
                backfill_payload,
            ) = await self._persist(
                organisation_id=organisation_id,
                integration_id=integration_id,
                opportunities=[],
                transitions=transitions,
                participant_extraction=participant_extraction,
            )
            self._merge_deal_write_result(write_result, side_write_result)

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
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
        )
        if external_ids is not None:
            if not external_ids:
                return {}
            stmt = stmt.where(col(CRMDeal.external_id).in_(external_ids))
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

    @staticmethod
    def _coerce_str(value: Any) -> str | None:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return None
