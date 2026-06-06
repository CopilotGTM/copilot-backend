"""HubSpot deals record sync orchestrator (phase 6).

Fresh-session lifecycle:

  Session A — load `KnowledgeIntegration` + `IntegrationTokenInfo`. Release.
  (no sess) — fetch the *complete* HubSpot deal property catalogue via
              `/crm/v3/properties/deals` (system + custom) and build the
              dynamic property list passed to the deals fetcher.
  (no sess) — paginated HubSpot deals fetch (with `propertiesWithHistory` and
              `associations=contacts,companies`).
  (no sess) — pure-Python normalise.
  Session B — load five session-scoped resolution maps from the dimensions
              already populated by phases 2-5 (pipelines, stages, users,
              companies, contacts), `writer.apply()`, commit.

Why fetch the property list dynamically: the user's explicit goal for this
phase is "capture the complete deal payload, not just two columns". Querying
the Properties API at sync time guarantees we pick up every property the
portal currently exposes — including custom fields added since phase 1 ran.
A baseline safety set drawn from `HUBSPOT_DEAL_CANONICAL_FIELDS` is unioned
in so a Properties API hiccup can never silently drop a column we depend on.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMPipeline,
)
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.deals.association_writer import (
    HubSpotDealAssociationWriter,
)
from src.service.integrations.crm.records.hubspot.deals.deal_normalizer import (
    extract_associations,
    extract_participants,
    extract_stage_transitions,
    normalize_deals,
)
from src.service.integrations.crm.records.hubspot.deals.deal_types import (
    AssociationWriteResult,
    DealResolutionMaps,
    DealSyncResult,
    DimensionHit,
    NormalizedDealAssociation,
    NormalizedDealParticipant,
    NormalizedHubSpotDeal,
    NormalizedStageTransition,
    ParticipantWriteResult,
    StageHistoryWriteResult,
)
from src.service.integrations.crm.records.hubspot.deals.deal_writer import HubSpotDealWriter
from src.service.integrations.crm.records.hubspot.deals.participant_writer import (
    HubSpotDealParticipantWriter,
)
from src.service.integrations.crm.records.hubspot.deals.stage_history_writer import (
    HubSpotDealStageHistoryWriter,
)
from src.service.integrations.crm.schema.hubspot.deals.mapping import (
    HUBSPOT_DEAL_CANONICAL_FIELDS,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.hubspot.deals_fetcher import (
    HubSpotDealsFetcher,
)
from src.workers.integrations_worker.providers.crm.hubspot.schema_fetcher import (
    HubSpotSchemaFetcher,
)

logger = get_logger(__name__)


# Properties we *always* request even if the Properties API doesn't list them
# — drawn from the canonical map (because losing those breaks downstream
# columns) and the change-history trackers (so phase 7 has data to read).
_BASELINE_HISTORY_PROPERTIES: tuple[str, ...] = (
    "dealstage",
    "pipeline",
    "amount",
    "closedate",
    "hs_deal_stage_probability",
    "hs_forecast_probability",
    "hs_forecast_amount",
)


class DealSyncConfigurationError(ValueError):
    """Integration not usable for deals record sync (missing, wrong type, inactive)."""


class HubSpotDealRecordSyncService:
    """Orchestrate one HubSpot deals record sync run."""

    PROVIDER = "hubspot"
    OBJECT_TYPE = "deal"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> DealSyncResult:
        """Run a full HubSpot deal sync for one org-scoped integration."""
        token_info = await self._load_token_info(organisation_id, integration_id)

        property_list = await self._build_property_list(token_info=token_info)

        raw_deals = await HubSpotDealsFetcher(
            token_info=token_info,
            properties=property_list,
        ).fetch_all()

        result = await self._persist_raw_deals(
            organisation_id=organisation_id,
            integration_id=integration_id,
            raw_deals=raw_deals,
            property_list_size=len(property_list),
        )
        logger.info(
            "HubSpot deals sync summary",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "fetched_deals": result.fetched_deals,
                "property_list_size": result.property_list_size,
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
                "skipped_malformed": result.skipped_malformed,
                "stage_history_created": result.stage_history_created,
                "stage_history_updated": result.stage_history_updated,
                "stage_history_unresolved_stage": result.stage_history_unresolved_stage,
                "stage_history_unresolved_from_stage": (result.stage_history_unresolved_from_stage),
                "participants_created": result.participants_created,
                "participants_updated": result.participants_updated,
                "participants_unresolved_related": result.participants_unresolved_related,
                "associations_created": result.associations_created,
                "associations_updated": result.associations_updated,
                "associations_unresolved_to_local": result.associations_unresolved_to_local,
            },
        )
        return result

    async def _persist_raw_deals(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_deals: list[dict[str, Any]],
        property_list_size: int = 0,
    ) -> DealSyncResult:
        """Persist raw HubSpot deal payloads from full sync or webhook fetches."""
        started_at = datetime.now(UTC)
        normalized, skipped_malformed = normalize_deals(raw_deals)

        # Extract sibling rows from the raw payload: stage transitions live in
        # `propertiesWithHistory.dealstage`, participants in the inline
        # associations block + collaborator/split owner properties, and
        # generic edges (incl. line_items) in the same associations block.
        # Doing this on the raw record means we get HubSpot's full ordering
        # and provenance fields without needing a re-fetch.
        transitions: list[NormalizedStageTransition] = []
        participants: list[NormalizedDealParticipant] = []
        edges: list[NormalizedDealAssociation] = []
        for raw in raw_deals:
            if not isinstance(raw, dict):
                continue
            transitions.extend(extract_stage_transitions(raw))
            participants.extend(extract_participants(raw))
            edges.extend(extract_associations(raw))

        (
            write_result,
            stage_history_result,
            participant_result,
            association_result,
        ) = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
            transitions=transitions,
            participants=participants,
            edges=edges,
        )

        completed_at = datetime.now(UTC)
        return DealSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_deals=len(raw_deals),
            property_list_size=property_list_size,
            deals_created=write_result.deals_created,
            deals_updated=write_result.deals_updated,
            deals_skipped_stale=write_result.deals_skipped_stale,
            unresolved_pipeline_link=write_result.unresolved_pipeline_link,
            unresolved_stage_link=write_result.unresolved_stage_link,
            unresolved_owner_link=write_result.unresolved_owner_link,
            unresolved_company_link=write_result.unresolved_company_link,
            unresolved_contact_link=write_result.unresolved_contact_link,
            unresolved_created_by_link=write_result.unresolved_created_by_link,
            unresolved_updated_by_link=write_result.unresolved_updated_by_link,
            skipped_malformed=skipped_malformed,
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
        )

    async def _load_token_info(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> IntegrationTokenInfo:
        """Load active HubSpot token context for one tenant integration."""
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                # The integration id can come from worker/webhook routing; the
                # organisation filter is the tenant boundary for token access.
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise DealSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.HUBSPOT:
                raise DealSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise DealSyncConfigurationError(f"Integration {integration_id} is not active")
            return IntegrationTokenInfo.from_integration(integration, organisation_id)
        raise DealSyncConfigurationError("Could not acquire org session for deals record sync")

    async def _build_property_list(
        self,
        *,
        token_info: IntegrationTokenInfo,
    ) -> list[str]:
        """Union of (live HubSpot property catalogue) and (canonical baseline).

        Querying `/crm/v3/properties/deals` here — instead of reading the
        cached `crm_custom_field_definitions` table — guarantees we pick up
        properties added between phase 1 (schema sync) and this phase. The
        baseline safety set protects us if the Properties API itself errors
        or returns an unexpectedly slim list.

        Falling back to the baseline *silently drops custom-field coverage*
        (phase 6's whole point is full capture). We retry the Properties API
        a couple of times with exponential backoff before giving up, since
        HubSpot's /properties endpoint can be slow-to-respond on large
        portals.
        """
        discovered = await self._fetch_property_names_with_retry(token_info=token_info)

        baseline = set(HUBSPOT_DEAL_CANONICAL_FIELDS.keys()) | set(_BASELINE_HISTORY_PROPERTIES)
        merged = sorted(discovered | baseline)
        logger.info(
            "Built HubSpot deal property list",
            extra={
                "integration_id": str(token_info.integration_id),
                "discovered_count": len(discovered),
                "baseline_count": len(baseline),
                "merged_count": len(merged),
                "used_fallback": len(discovered) == 0,
            },
        )
        return merged

    async def _fetch_property_names_with_retry(
        self,
        *,
        token_info: IntegrationTokenInfo,
    ) -> set[str]:
        """Three attempts with 2s / 5s backoff on timeout. Returns empty set
        if all attempts fail so the caller can fall back to the baseline.

        Only retries on network / timeout errors. An auth error or malformed
        response raises out immediately — retrying those would just mask the
        real problem.
        """
        max_attempts = 3
        backoff_schedule = (2.0, 5.0)
        for attempt in range(1, max_attempts + 1):
            try:
                raw = await HubSpotSchemaFetcher(token_info=token_info).fetch_properties(
                    object_type="deals"
                )
                return {
                    entry["name"]
                    for entry in raw
                    if isinstance(entry, dict)
                    and isinstance(entry.get("name"), str)
                    and entry["name"]
                }
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                if attempt == max_attempts:
                    logger.exception(
                        "HubSpot deal properties API failed after retries; falling back to baseline",
                        extra={
                            "integration_id": str(token_info.integration_id),
                            "attempts": attempt,
                        },
                    )
                    return set()
                delay = backoff_schedule[attempt - 1]
                logger.warning(
                    "HubSpot deal properties API transient failure; retrying",
                    extra={
                        "integration_id": str(token_info.integration_id),
                        "attempt": attempt,
                        "next_delay_seconds": delay,
                        "error_class": type(exc).__name__,
                    },
                )
                await asyncio.sleep(delay)
            except Exception:
                logger.exception(
                    "HubSpot deal properties API non-retryable failure; falling back to baseline",
                    extra={"integration_id": str(token_info.integration_id)},
                )
                return set()
        return set()

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        normalized: list[NormalizedHubSpotDeal],
        transitions: list[NormalizedStageTransition],
        participants: list[NormalizedDealParticipant],
        edges: list[NormalizedDealAssociation],
    ) -> tuple[Any, StageHistoryWriteResult, ParticipantWriteResult, AssociationWriteResult]:
        """Write deals and their sibling rows in one tenant-scoped transaction."""
        async for session in acquire_org_async_session(organisation_id):
            maps = await self._load_resolution_maps(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            writer = HubSpotDealWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(normalized=normalized, maps=maps)

            # Deals are flushed (writer.apply runs flush internally), so PKs
            # are now visible. Build {deal_external_id → deal_id} map for the
            # three sibling writers — they all need it as their from-side.
            deal_pk_map = await self._load_deal_pk_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )

            stage_history_result = await HubSpotDealStageHistoryWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            ).apply(
                transitions=transitions,
                deal_pk_map=deal_pk_map,
                stage_map=maps.stage_map,
            )

            participant_result = await HubSpotDealParticipantWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            ).apply(
                participants=participants,
                deal_pk_map=deal_pk_map,
                contact_map=maps.contact_map,
                company_map=maps.company_map,
                user_map=maps.user_map,
            )

            association_result = await HubSpotDealAssociationWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            ).apply(
                edges=edges,
                deal_pk_map=deal_pk_map,
                contact_map=maps.contact_map,
                company_map=maps.company_map,
            )

            await session.commit()
            return (
                write_result,
                stage_history_result,
                participant_result,
                association_result,
            )

        raise DealSyncConfigurationError(
            "Could not acquire org session for deals record sync write"
        )

    async def _load_deal_pk_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, UUID]:
        """`{external_id → deal_id}` for sibling writers (stage history,
        participants, associations) that need to set the deal-side FK."""
        external_to_local = await self._load_external_to_local(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.DEAL,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
            col(CRMDeal.deal_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).scalars().all()
        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        return {
            local_to_external[row.deal_id]: row.deal_id
            for row in rows
            if row.deal_id in local_to_external
        }

    async def _load_resolution_maps(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> DealResolutionMaps:
        """Build all five maps the writer consults for FK + display resolution.

        Each map is scoped to `(organisation_id, integration_id)` so a deal
        from one HubSpot portal can never accidentally bind to a dimension
        row from a different portal in the same org.
        """
        pipeline_map: dict[str, DimensionHit] = {}
        stage_map: dict[str, DimensionHit] = {}
        user_map: dict[str, DimensionHit] = {}
        company_map: dict[str, DimensionHit] = {}
        contact_map: dict[str, DimensionHit] = {}

        pipeline_ids = await self._load_external_to_local(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.PIPELINE,
        )
        stage_ids = await self._load_external_to_local(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.STAGE,
        )
        user_ids = await self._load_external_to_local(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.USER,
        )
        company_ids = await self._load_external_to_local(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.COMPANY,
        )
        contact_ids = await self._load_external_to_local(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.CONTACT,
        )

        # Pipelines
        pipeline_stmt = select(CRMPipeline).where(
            col(CRMPipeline.organisation_id) == organisation_id,
            col(CRMPipeline.integration_id) == integration_id,
            col(CRMPipeline.is_deleted) == False,  # noqa: E712
            col(CRMPipeline.pipeline_id).in_(set(pipeline_ids.values())),
        )
        pipeline_local_to_external = _local_to_external(pipeline_ids)
        for pipeline in (await session.execute(pipeline_stmt)).scalars().all():
            external_id = pipeline_local_to_external.get(pipeline.pipeline_id)
            if external_id:
                pipeline_map[external_id] = DimensionHit(
                    internal_id=pipeline.pipeline_id,
                    name=pipeline.name,
                    label=pipeline.label,
                )

        # Stages — note pipeline_id is captured so the writer can fall back
        # to it when HubSpot's deal `pipeline` property is missing.
        stage_stmt = select(CRMDealStage).where(
            col(CRMDealStage.organisation_id) == organisation_id,
            col(CRMDealStage.integration_id) == integration_id,
            col(CRMDealStage.is_deleted) == False,  # noqa: E712
            col(CRMDealStage.stage_id).in_(set(stage_ids.values())),
        )
        stage_local_to_external = _local_to_external(stage_ids)
        for stage in (await session.execute(stage_stmt)).scalars().all():
            external_id = stage_local_to_external.get(stage.stage_id)
            if external_id:
                stage_map[external_id] = DimensionHit(
                    internal_id=stage.stage_id,
                    name=stage.name,
                    label=stage.label,
                    pipeline_id=stage.pipeline_id,
                )

        # Users — single map covers owner_id + created_by_user_id +
        # updated_by_user_id. Phase 3's identity-bridge guarantees every
        # HubSpot owner has a row here (matched or freshly minted UUID).
        user_stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
            col(CRMUser.user_id).in_(set(user_ids.values())),
        )
        user_local_to_external = _local_to_external(user_ids)
        for user in (await session.execute(user_stmt)).scalars().all():
            external_id = user_local_to_external.get(user.user_id)
            if external_id:
                user_map[external_id] = DimensionHit(
                    internal_id=user.user_id,
                    name=user.full_name,
                    email=user.email,
                )

        # Companies
        company_stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
            col(CRMCompany.company_id).in_(set(company_ids.values())),
        )
        company_local_to_external = _local_to_external(company_ids)
        for company in (await session.execute(company_stmt)).scalars().all():
            external_id = company_local_to_external.get(company.company_id)
            if external_id:
                company_map[external_id] = DimensionHit(
                    internal_id=company.company_id,
                    name=company.name,
                    domain=company.domain,
                )

        # Contacts
        contact_stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
            col(CRMContact.contact_id).in_(set(contact_ids.values())),
        )
        contact_local_to_external = _local_to_external(contact_ids)
        for contact in (await session.execute(contact_stmt)).scalars().all():
            external_id = contact_local_to_external.get(contact.contact_id)
            if external_id:
                contact_map[external_id] = DimensionHit(
                    internal_id=contact.contact_id,
                    name=contact.full_name,
                    email=contact.email,
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
            },
        )
        return DealResolutionMaps(
            pipeline_map=pipeline_map,
            stage_map=stage_map,
            user_map=user_map,
            company_map=company_map,
            contact_map=contact_map,
        )

    async def _load_external_to_local(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        object_type: CRMSourceObjectType,
    ) -> dict[str, UUID]:
        helper = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider=self.PROVIDER,
        )
        return await helper.load_local_id_map(object_type=object_type)


def _local_to_external(external_to_local: dict[str, UUID]) -> dict[UUID, str]:
    return {local_id: external_id for external_id, local_id in external_to_local.items()}
