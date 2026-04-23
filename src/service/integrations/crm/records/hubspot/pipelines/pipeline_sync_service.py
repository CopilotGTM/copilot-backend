"""HubSpot deal pipelines + stages record sync orchestrator.

Fresh-session lifecycle (mirrors schema sync):

  Session A  — load KnowledgeIntegration, extract IntegrationTokenInfo, release.
  (no session) — fetch HubSpot pipelines via the Pipelines API.
  (no session) — pure-Python normalize pass.
  Session B  — writer.apply() → upsert + deactivation, commit, release.

The Tier 3 Celery task that wraps this service owns sync-history and
connection-metadata sessions (see tasks/crm/hubspot_pipeline_record_sync_task.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.pipelines.pipeline_normalizer import (
    normalize_pipelines,
)
from src.service.integrations.crm.records.hubspot.pipelines.pipeline_types import (
    PipelineSyncResult,
)
from src.service.integrations.crm.records.hubspot.pipelines.pipeline_writer import (
    HubSpotPipelineWriter,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.hubspot.pipelines_fetcher import (
    HubSpotPipelinesFetcher,
)

logger = get_logger(__name__)


class PipelineSyncConfigurationError(ValueError):
    """Integration not usable for pipelines record sync (missing, wrong type, inactive)."""


class HubSpotPipelineRecordSyncService:
    """Orchestrate one HubSpot deal-pipelines record sync run."""

    PROVIDER = "hubspot"
    OBJECT_TYPE = "pipeline"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> PipelineSyncResult:
        started_at = datetime.now(UTC)

        token_info = await self._load_token_info(organisation_id, integration_id)

        raw_pipelines = await HubSpotPipelinesFetcher(token_info=token_info).fetch_deal_pipelines()

        normalized, skipped_pipelines, skipped_stages = normalize_pipelines(raw_pipelines)
        fetched_stage_count = sum(
            1 for raw in raw_pipelines if isinstance(raw, dict) for _ in (raw.get("stages") or [])
        )

        write_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            pipelines=normalized,
        )

        completed_at = datetime.now(UTC)
        return PipelineSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_pipelines=len(raw_pipelines),
            fetched_stages=fetched_stage_count,
            pipelines_created=write_result.pipelines_created,
            pipelines_updated=write_result.pipelines_updated,
            pipelines_reactivated=write_result.pipelines_reactivated,
            pipelines_deactivated=write_result.pipelines_deactivated,
            stages_created=write_result.stages_created,
            stages_updated=write_result.stages_updated,
            stages_reactivated=write_result.stages_reactivated,
            stages_deactivated=write_result.stages_deactivated,
            skipped_pipelines=skipped_pipelines,
            skipped_stages=skipped_stages,
        )

    async def _load_token_info(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> IntegrationTokenInfo:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise PipelineSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.HUBSPOT:
                raise PipelineSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise PipelineSyncConfigurationError(f"Integration {integration_id} is not active")
            return IntegrationTokenInfo.from_integration(integration, organisation_id)
        raise PipelineSyncConfigurationError(
            "Could not acquire org session for pipelines record sync"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        pipelines,
    ):
        async for session in acquire_org_async_session(organisation_id):
            writer = HubSpotPipelineWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(pipelines=pipelines)
            await session.commit()
            return write_result
        raise PipelineSyncConfigurationError("Could not acquire org session for pipelines write")
