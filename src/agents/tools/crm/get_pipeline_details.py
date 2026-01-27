"""Get Pipeline Details Tool - Available pipelines and their stages.

This tool retrieves all available CRM pipelines and their stages for an
organisation, showing the complete sales process structure.

USE CASE:
- Understand the sales process and available pipelines
- Get stage progression and probability information
- Identify which stages are open, won, or lost
- Plan deal movement through the pipeline

RETURNS:
- List of all active pipelines
- For each pipeline: stages with order, state, and probability
- Stage classification (open/won/lost)

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access (cannot be passed as parameter)

This is an ORG-SCOPED tool. organisation_id comes from context to ensure
users can only access their own organisation's pipelines.
deal_id is NOT required for this tool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlmodel import col

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMPipeline,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_pipeline_details(
    *,
    context: AgentContext,
    pipeline_id: str | None = None,
    include_deal_counts: bool = False,
    include_inactive: bool = False,
) -> dict[str, Any] | None:
    """Get pipeline and stage details for an organisation.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access (not a parameter)

    Args:
        context: AgentContext containing organisation metadata (organisation_id from here)
        pipeline_id: Optional specific pipeline UUID to fetch (fetches all if None)
        include_deal_counts: Include count of deals in each stage (default: False)
        include_inactive: Include inactive pipelines (default: False)

    Returns:
        Dictionary with pipelines and their stages, or None if error.
    """
    # SECURITY: organisation_id comes from context only, never from parameters
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("get_pipeline_details: missing organisation_id in context")
        return None

    pipeline_c = cast(Any, CRMPipeline).__table__.c
    stage_c = cast(Any, CRMDealStage).__table__.c

    async with context.acquire_session() as session:
        # Build pipeline query - select only required columns
        pipeline_filters = [
            pipeline_c.organisation_id == organisation_id,
            pipeline_c.is_deleted.is_(False),
        ]

        if not include_inactive:
            pipeline_filters.append(pipeline_c.is_active.is_(True))

        if pipeline_id:
            try:
                parsed_pipeline_id = UUID(pipeline_id)
                pipeline_filters.append(pipeline_c.pipeline_id == parsed_pipeline_id)
            except ValueError:
                logger.warning("get_pipeline_details: invalid pipeline_id format: %s", pipeline_id)
                return None

        # Select only required pipeline columns (no payload, sync metadata, etc.)
        pipeline_stmt = (
            select(
                col(CRMPipeline.pipeline_id),
                col(CRMPipeline.name),
                col(CRMPipeline.label),
                col(CRMPipeline.object_type),
                col(CRMPipeline.is_default),
                col(CRMPipeline.is_active),
                col(CRMPipeline.display_order),
            )
            .where(*pipeline_filters)
            .order_by(pipeline_c.display_order.asc().nulls_last(), pipeline_c.name.asc())
        )

        try:
            pipeline_result = await session.execute(pipeline_stmt)
            pipeline_rows = pipeline_result.all()
        except Exception as e:
            logger.exception(f"get_pipeline_details: error fetching pipelines: {e}")
            return None

        if not pipeline_rows:
            logger.info("get_pipeline_details: no pipelines found")
            return {"pipelines": [], "total_pipelines": 0}

        # Get all stages for these pipelines - select only required columns
        pipeline_ids = [p.pipeline_id for p in pipeline_rows]

        stage_stmt = (
            select(
                col(CRMDealStage.stage_id),
                col(CRMDealStage.pipeline_id),
                col(CRMDealStage.name),
                col(CRMDealStage.label),
                col(CRMDealStage.stage_key),
                col(CRMDealStage.stage_state),
                col(CRMDealStage.probability),
                col(CRMDealStage.forecast_category),
                col(CRMDealStage.is_closed),
                col(CRMDealStage.display_order),
            )
            .where(
                stage_c.pipeline_id.in_(pipeline_ids),
                stage_c.is_deleted.is_(False),
            )
            .order_by(stage_c.display_order.asc().nulls_last(), stage_c.name.asc())
        )

        try:
            stage_result = await session.execute(stage_stmt)
            all_stage_rows = stage_result.all()
        except Exception as e:
            logger.exception(f"get_pipeline_details: error fetching stages: {e}")
            all_stage_rows = []

        # Group stages by pipeline
        stages_by_pipeline: dict[UUID, list[Any]] = {}
        for stage_row in all_stage_rows:
            if stage_row.pipeline_id not in stages_by_pipeline:
                stages_by_pipeline[stage_row.pipeline_id] = []
            stages_by_pipeline[stage_row.pipeline_id].append(stage_row)

        # Optionally get deal counts per stage
        deal_counts_by_stage: dict[UUID, int] = {}
        if include_deal_counts:
            deal_c = cast(Any, CRMDeal).__table__.c
            count_stmt = (
                select(
                    col(CRMDeal.stage_id),
                    func.count(col(CRMDeal.deal_id)).label("deal_count"),
                )
                .where(
                    deal_c.organisation_id == organisation_id,
                    deal_c.is_deleted.is_(False),
                    deal_c.stage_id.isnot(None),
                )
                .group_by(col(CRMDeal.stage_id))
            )

            try:
                count_result = await session.execute(count_stmt)
                for row in count_result:
                    if row.stage_id:
                        deal_counts_by_stage[row.stage_id] = row.deal_count
            except Exception as e:
                logger.warning(f"get_pipeline_details: error fetching deal counts: {e}")

        # Format output
        pipelines_output: list[dict[str, Any]] = []
        for pipeline_row in pipeline_rows:
            pipeline_stages = stages_by_pipeline.get(pipeline_row.pipeline_id, [])

            stages_output: list[dict[str, Any]] = []
            for stage_row in pipeline_stages:
                stage_entry: dict[str, Any] = {
                    "stage_id": str(stage_row.stage_id),
                    "name": stage_row.name,
                    "label": stage_row.label,
                    "stage_key": stage_row.stage_key,
                    "stage_state": (
                        stage_row.stage_state.value
                        if hasattr(stage_row.stage_state, "value")
                        else stage_row.stage_state
                    ),
                    "probability": stage_row.probability,
                    "forecast_category": stage_row.forecast_category,
                    "is_closed": stage_row.is_closed,
                    "display_order": stage_row.display_order,
                }

                if include_deal_counts:
                    stage_entry["deal_count"] = deal_counts_by_stage.get(stage_row.stage_id, 0)

                stages_output.append(stage_entry)

            pipeline_entry: dict[str, Any] = {
                "pipeline_id": str(pipeline_row.pipeline_id),
                "name": pipeline_row.name,
                "label": pipeline_row.label,
                "object_type": pipeline_row.object_type,
                "is_default": pipeline_row.is_default,
                "is_active": pipeline_row.is_active,
                "display_order": pipeline_row.display_order,
                "stages": stages_output,
                "total_stages": len(stages_output),
            }

            pipelines_output.append(pipeline_entry)

        output: dict[str, Any] = {
            "pipelines": pipelines_output,
            "total_pipelines": len(pipelines_output),
        }

    logger.info(
        "get_pipeline_details: found %d pipelines with %d total stages",
        len(pipelines_output),
        sum(len(p["stages"]) for p in pipelines_output),
    )
    return output


def create_get_pipeline_details(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_pipeline_details tool bound to a context.

    SECURITY: organisation_id comes from context (immutable, not a tool parameter).
    The agent cannot pass organisation_id. deal_id is not required for this tool.
    """

    async def get_pipeline_details_tool(
        pipeline_id: str | None = None,
        include_deal_counts: bool = False,
        include_inactive: bool = False,
    ) -> dict[str, Any] | None:
        """Get pipeline and stage details for the organisation.

        Use this tool to understand the available sales pipelines and their
        stages. Shows the complete pipeline structure including stage
        progression, probabilities, and state classification.

        Note: deal_id is NOT required for this tool. It returns all pipelines
        available to the organisation.

        Args:
            pipeline_id: Optional specific pipeline UUID (fetches all if not provided)
            include_deal_counts: Include count of deals in each stage (default: False)
            include_inactive: Include inactive pipelines (default: False)

        Returns:
            - pipelines: List of pipelines, each containing:
                - pipeline_id, name, label
                - object_type (e.g., "deals")
                - is_default, is_active, display_order
                - stages: List of stages with:
                    - stage_id, name, label, stage_key
                    - stage_state: "open", "won", or "lost"
                    - probability: Win probability percentage (0-100)
                    - forecast_category
                    - is_closed: Whether this is a closed stage
                    - display_order
                    - deal_count (if include_deal_counts=True)
                - total_stages: Number of stages in the pipeline
            - total_pipelines: Number of pipelines returned
        """
        logger.info(
            "[TOOL CALLED] get_pipeline_details - pipeline_id='%s', include_deal_counts=%s",
            pipeline_id,
            include_deal_counts,
        )

        if not context.organisation_id:
            logger.warning("get_pipeline_details unavailable: missing organisation_id in context")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetPipelineDetails",
            tool_func=get_pipeline_details,
            parameters={
                "context": context,
                "pipeline_id": pipeline_id,
                "include_deal_counts": include_deal_counts,
                "include_inactive": include_inactive,
            },
            description="Getting pipeline and stage details",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] get_pipeline_details found %d pipelines",
                result.get("total_pipelines", 0),
            )
        else:
            logger.info("[TOOL RESULT] get_pipeline_details returned None (error)")

        return dict(result) if result else None

    return get_pipeline_details_tool


__all__ = [
    "get_pipeline_details",
    "create_get_pipeline_details",
]
