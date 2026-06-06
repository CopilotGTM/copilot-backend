"""Get Deal Change History Tool - Stage transition history for a deal.

This tool retrieves the complete stage change history for a specific deal,
showing how the deal has progressed through different pipeline stages over time.

USE CASE:
- Understand deal progression and velocity
- Identify stalled deals or unusual stage transitions
- Analyze time spent in each stage
- Review deal lifecycle for reporting

RETURNS:
- List of stage transitions with timestamps
- From/to stage information
- Time spent in each stage (if available)
- Amount and probability snapshots at each transition

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access (cannot be passed as parameter)

This is an ORG-SCOPED tool. deal_id is a parameter, but organisation_id
comes from context to ensure users can only access their own deals.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMDealStageHistory,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_deal_change_history(
    deal_id: str,
    *,
    context: AgentContext,
    limit: int = 50,
) -> dict[str, Any] | None:
    """Get stage transition history for a deal.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access (not a parameter)

    Args:
        deal_id: The deal UUID as a string (passed by agent)
        context: AgentContext containing organisation metadata (organisation_id from here)
        limit: Maximum number of history records to return (default: 50)

    Returns:
        Dictionary with deal info and stage change history, or None if not found.
    """
    # SECURITY: organisation_id comes from context only, never from parameters
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("get_deal_change_history: missing organisation_id in context")
        return None

    try:
        parsed_deal_id = UUID(deal_id)
    except ValueError:
        logger.warning("get_deal_change_history: invalid deal_id format: %s", deal_id)
        return None

    deal_c = cast(Any, CRMDeal).__table__.c
    history_c = cast(Any, CRMDealStageHistory).__table__.c
    stage_c = cast(Any, CRMDealStage).__table__.c

    async with context.acquire_session() as session:
        # First, get only the required deal columns to verify access and get basic info
        deal_stmt = select(
            col(CRMDeal.deal_id),
            col(CRMDeal.name),
            col(CRMDeal.company_name),
            col(CRMDeal.stage_name),
            col(CRMDeal.stage_label),
            col(CRMDeal.stage_entered_at),
            col(CRMDeal.pipeline_name),
            col(CRMDeal.pipeline_label),
        ).where(
            deal_c.organisation_id == organisation_id,
            deal_c.deal_id == parsed_deal_id,
            deal_c.is_deleted.is_(False),
        )

        try:
            deal_result = await session.execute(deal_stmt)
            deal_row = deal_result.one_or_none()
        except Exception as e:
            logger.exception(f"get_deal_change_history: error fetching deal: {e}")
            return None

        if not deal_row:
            logger.info("get_deal_change_history: deal not found: %s", deal_id)
            return None

        # Get only required stage history columns, ordered by entered_at descending
        history_stmt = (
            select(
                col(CRMDealStageHistory.history_id),
                col(CRMDealStageHistory.stage_id),
                col(CRMDealStageHistory.from_stage_id),
                col(CRMDealStageHistory.entered_at),
                col(CRMDealStageHistory.exited_at),
                col(CRMDealStageHistory.elapsed_seconds),
                col(CRMDealStageHistory.amount_at_stage),
                col(CRMDealStageHistory.probability_at_stage),
                col(CRMDealStageHistory.source),
            )
            .where(
                history_c.organisation_id == organisation_id,
                history_c.deal_id == parsed_deal_id,
            )
            .order_by(history_c.entered_at.desc())
            .limit(limit)
        )

        try:
            history_result = await session.execute(history_stmt)
            history_rows = history_result.all()
        except Exception as e:
            logger.exception(f"get_deal_change_history: error fetching history: {e}")
            return None

        # Build a map of stage_id to stage info for enrichment (only required columns)
        stage_ids = {row.stage_id for row in history_rows if row.stage_id is not None}

        stage_map: dict[UUID, Any] = {}
        if stage_ids:
            stage_stmt = select(
                col(CRMDealStage.stage_id),
                col(CRMDealStage.name),
                col(CRMDealStage.label),
                col(CRMDealStage.stage_state),
                col(CRMDealStage.is_closed),
            ).where(stage_c.stage_id.in_(stage_ids))
            try:
                stage_result = await session.execute(stage_stmt)
                for stage_row in stage_result.all():
                    stage_map[stage_row.stage_id] = stage_row
            except Exception as e:
                logger.warning(f"get_deal_change_history: error fetching stages: {e}")

        # Format the history records
        changes: list[dict[str, Any]] = []
        for row in history_rows:
            stage_info = stage_map.get(row.stage_id) if row.stage_id else None

            change_entry: dict[str, Any] = {
                "entered_at": row.entered_at.isoformat() if row.entered_at else None,
                "exited_at": row.exited_at.isoformat() if row.exited_at else None,
                "from_stage_id": str(row.from_stage_id) if row.from_stage_id else None,
                "elapsed_seconds": row.elapsed_seconds,
                "elapsed_days": round(row.elapsed_seconds / 86400, 1)
                if row.elapsed_seconds
                else None,
                "amount_at_stage": float(row.amount_at_stage) if row.amount_at_stage else None,
                "probability_at_stage": row.probability_at_stage,
                "source": row.source,
            }

            # Enrich with stage details if available
            if stage_info:
                change_entry["stage_name"] = stage_info.name
                change_entry["stage_label"] = stage_info.label
                change_entry["stage_state"] = (
                    stage_info.stage_state.value
                    if hasattr(stage_info.stage_state, "value")
                    else stage_info.stage_state
                )
                change_entry["is_closed"] = stage_info.is_closed
            changes.append(change_entry)

        # Calculate summary statistics
        total_changes = len(changes)
        first_stage_at = changes[-1]["entered_at"] if changes else None
        last_stage_at = changes[0]["entered_at"] if changes else None

        output: dict[str, Any] = {
            "deal_id": str(deal_row.deal_id),
            "deal_name": deal_row.name,
            "company_name": deal_row.company_name,
            "current_stage": {
                "name": deal_row.stage_name,
                "label": deal_row.stage_label,
                "entered_at": deal_row.stage_entered_at.isoformat()
                if deal_row.stage_entered_at
                else None,
            },
            "current_pipeline": {
                "name": deal_row.pipeline_name,
                "label": deal_row.pipeline_label,
            },
            "stage_changes": changes,
            "summary": {
                "total_stage_changes": total_changes,
                "first_stage_at": first_stage_at,
                "last_stage_at": last_stage_at,
            },
        }

    logger.info(
        "get_deal_change_history: found %d stage changes for deal %s",
        total_changes,
        deal_id,
    )
    return output


def create_get_deal_change_history(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_deal_change_history tool bound to a context.

    SECURITY: organisation_id comes from context (immutable, not a tool parameter).
    The agent can pass deal_id but NOT organisation_id.
    """

    async def get_deal_change_history_tool(
        deal_id: str,
        limit: int = 50,
    ) -> dict[str, Any] | None:
        """Get the stage change history for a deal.

        Use this tool to understand how a deal has progressed through
        different pipeline stages over time. Shows all stage transitions
        with timestamps and time spent in each stage.

        Args:
            deal_id: The deal UUID (from search_deals or list_deals)
            limit: Maximum number of history records to return (default: 50)

        Returns:
            - deal_id, deal_name, company_name
            - current_stage: Current stage name, label, and when entered
            - current_pipeline: Pipeline name and label
            - stage_changes: List of stage transitions with:
                - entered_at, exited_at timestamps
                - stage_name, stage_label, stage_state
                - elapsed_seconds/elapsed_days in stage
                - amount_at_stage, probability_at_stage snapshots
                - source of the change (api, workflow, user)
            - summary: total_stage_changes, first_stage_at, last_stage_at
        """
        logger.info("[TOOL CALLED] get_deal_change_history - deal_id='%s'", deal_id)

        if not context.organisation_id:
            logger.warning(
                "get_deal_change_history unavailable: missing organisation_id in context"
            )
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetDealChangeHistory",
            tool_func=get_deal_change_history,
            parameters={
                "deal_id": deal_id,
                "context": context,
                "limit": limit,
            },
            description="Checking deal stage history",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] get_deal_change_history found %d changes for deal: %s",
                result.get("summary", {}).get("total_stage_changes", 0),
                result.get("deal_name", "Unknown"),
            )
        else:
            logger.info("[TOOL RESULT] get_deal_change_history returned None (not found)")

        return dict(result) if result else None

    return get_deal_change_history_tool


__all__ = [
    "get_deal_change_history",
    "create_get_deal_change_history",
]
