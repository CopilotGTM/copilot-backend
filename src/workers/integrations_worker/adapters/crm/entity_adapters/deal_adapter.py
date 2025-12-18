"""Deal entity adapter for CRM sync."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.entities.deal_sync_service import DealSyncService
from src.service.integrations.crm.entities.pipeline_sync_service import PipelineSyncService

logger = get_logger(__name__)


async def sync_deals(
    *,
    db: AsyncSession,
    organisation_id: str,
    integration: KnowledgeIntegration,
    records: list[dict[str, Any]],
    provider: str,
    provider_config_key: str | None = None,
) -> dict[str, Any]:
    """Sync deal records using DealSyncService.

    After syncing deals, automatically syncs pipelines to ensure pipeline/stage
    names are populated in deal records.

    Args:
        db: Database session
        organisation_id: Organisation UUID string
        integration: KnowledgeIntegration instance
        records: Raw deal records from provider
        provider: Provider name (e.g., 'hubspot')
        provider_config_key: Nango provider config key (optional, will be derived if not provided)

    Returns:
        Sync statistics including deals, participants, stage history, and pipelines
    """
    # Step 1: Sync deals
    service = DealSyncService(db=db, organisation_id=UUID(organisation_id))
    deal_result = await service.sync(
        integration=integration,
        records=records,
        provider=provider,
    )

    logger.info(
        "Deals sync completed: %d deals processed. Triggering pipelines sync...",
        deal_result.get("deals", 0),
    )

    # Step 2: Auto-sync pipelines to populate pipeline/stage names
    # This ensures deals have enriched pipeline_name and stage_name fields
    try:
        # Determine provider_config_key if not provided
        if not provider_config_key:
            # Convert string to enum if needed (SQLAlchemy returns string from db)
            integration_type = (
                IntegrationType(integration.integration_type)
                if isinstance(integration.integration_type, str)
                else integration.integration_type
            )
            provider_config_key = to_nango_connect_integration_key(integration_type)

        pipeline_result = await _auto_sync_pipelines(
            db=db,
            organisation_id=organisation_id,
            integration=integration,
            provider=provider,
            provider_config_key=provider_config_key,
        )
        logger.info(
            "Auto-sync pipelines completed: %d pipelines, %d stages, %d deals enriched",
            pipeline_result.get("pipelines", 0),
            pipeline_result.get("stages", 0),
            pipeline_result.get("deals_enriched", 0),
        )

        # Merge results
        deal_result["auto_synced_pipelines"] = pipeline_result.get("pipelines", 0)
        deal_result["auto_synced_stages"] = pipeline_result.get("stages", 0)
        deal_result["deals_enriched"] = pipeline_result.get("deals_enriched", 0)

    except Exception as exc:
        logger.warning(
            "Auto-sync pipelines failed (non-critical): %s",
            exc,
            exc_info=True,
        )
        deal_result["auto_sync_pipelines_error"] = str(exc)

    return deal_result


async def _auto_sync_pipelines(
    *,
    db: AsyncSession,
    organisation_id: str,
    integration: KnowledgeIntegration,
    provider: str,
    provider_config_key: str,
) -> dict[str, Any]:
    """Automatically fetch and sync pipelines after deals sync.

    Args:
        db: Database session
        organisation_id: Organisation UUID string
        integration: KnowledgeIntegration instance
        provider: Provider name (e.g., 'hubspot')
        provider_config_key: Nango provider config key (e.g., 'hubspot-copilotgtm')

    Returns:
        Sync statistics for pipelines
    """
    from src.service.integrations import NangoClient
    from src.service.integrations.nango_client import NangoConnectionError
    from src.workers.integrations_worker.providers.registry import get_fetcher

    logger.info(
        "Fetching pipelines from %s (provider_config_key: %s)...", provider, provider_config_key
    )

    # Fetch pipelines.
    #
    # Preferred path (legacy): Nango action `fetch-pipelines`.
    # Fallback path: provider fetcher via our registry (SOQL via Nango proxy).
    nango = NangoClient()
    try:
        if not integration.connection_id:
            logger.warning("No connection_id for integration %s", integration.integration_id)
            return {"pipelines": 0, "stages": 0, "error": "no_connection_id"}

        response: Any
        try:
            response = await nango.action(
                provider_config_key=provider_config_key,
                connection_id=integration.connection_id,
                action_name="fetch-pipelines",
            )
        except NangoConnectionError as exc:
            # Nango returns 404 "Action not found" when the action isn't configured.
            # Salesforce pipelines are already supported via our provider fetcher.
            if getattr(exc, "status_code", None) == 404:
                fetcher = get_fetcher("crm", provider, "pipelines")
                if not fetcher:
                    raise

                response = await fetcher(
                    nango,
                    integration.connection_id,
                    True,
                    None,
                    "pipelines",
                    provider_config_key=provider_config_key,
                )
            else:
                raise

        # Log the raw response structure for debugging
        logger.info("Nango action response type: %s", type(response))
        logger.info(
            "Nango action response keys: %s",
            response.keys() if isinstance(response, dict) else "not a dict",
        )

        # Extract pipelines from response
        # Handle different response structures:
        # 1. Direct array: [{pipeline}, ...]
        # 2. Wrapped: {"pipelines": [{pipeline}, ...]}
        # 3. Data envelope: {"data": {"pipelines": [...]}}
        if isinstance(response, list):
            pipelines = response
            logger.info("Response is a direct array with %d items", len(pipelines))
        elif isinstance(response, dict):
            # Try common keys
            pipelines = (
                response.get("pipelines")
                or response.get("data", {}).get("pipelines")
                or response.get("results")
                or []
            )
            logger.info("Extracted pipelines from dict response: %d items", len(pipelines))
        else:
            pipelines = []
            logger.warning("Unexpected response type: %s", type(response))

        if not pipelines:
            logger.warning(
                "No pipelines found in Nango action response. Response: %s",
                str(response)[:500],  # Log first 500 chars
            )
            return {"pipelines": 0, "stages": 0}

        logger.info("Fetched %d pipelines from Nango", len(pipelines))

        # Sync pipelines using PipelineSyncService
        pipeline_service = PipelineSyncService(db=db, organisation_id=UUID(organisation_id))
        result = await pipeline_service.sync(
            integration=integration,
            records=pipelines,
            provider=provider,
        )

        logger.info(
            "Pipeline sync completed: %d pipelines, %d stages. Enriching deals...",
            result.get("pipelines", 0),
            result.get("stages", 0),
        )

        # Enrich deals with pipeline/stage names after pipeline sync
        # This backfills pipeline_name, pipeline_label, stage_name, stage_label
        # for deals that were synced before pipelines existed
        try:
            deals_enriched = await pipeline_service.enrich_deals_with_pipeline_metadata(
                integration_id=integration.integration_id
            )
            result["deals_enriched"] = deals_enriched
            logger.info(
                "Successfully enriched %d deals with pipeline/stage metadata", deals_enriched
            )
        except Exception as enrich_exc:
            logger.warning(
                "Deal enrichment failed (non-critical): %s",
                enrich_exc,
                exc_info=True,
            )
            result["deals_enrichment_error"] = str(enrich_exc)

        return result

    except Exception as exc:
        logger.error("Failed to fetch/sync pipelines: %s", exc, exc_info=True)
        raise
    finally:
        await nango.close()
