"""Get Deal Memory Tool - Retrieve persistent deal memory document.

This tool fetches the accumulated deal memory for the current deal context.
The memory is a markdown document built by the DealMemoryAgent containing
synthesised knowledge from meetings, emails, and notes.

USE CASE:
When an agent needs pre-built context about a deal before performing
analysis (deal summary, risk extraction, stakeholder analysis, briefing).
Reading the deal memory first reduces redundant tool calls and provides
a holistic starting point.

RETURNS:
- content: The markdown deal memory document
- version: Document version number
- token_count: Approximate token count of the content

If no memory exists yet, returns a NOT_AVAILABLE status.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- deal_id: Scoped to the current deal context

This is a DEAL-SCOPED tool. Both organisation_id and deal_id come from context.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.deal_memory_models import DealMemory

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def get_deal_memory(
    *,
    context: AgentContext,
) -> dict[str, Any]:
    """Get the persistent deal memory for the current deal.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Scoped to current deal

    Args:
        context: AgentContext containing organisation and deal metadata

    Returns:
        Dictionary with memory content, version, and token count,
        or a NOT_AVAILABLE status if no memory exists.
    """
    organisation_id = context.organisation_id
    deal_id = context.deal_id

    if not organisation_id or not deal_id:
        logger.warning("get_deal_memory: missing organisation_id or deal_id in context")
        return {"status": "NOT_AVAILABLE", "message": "Missing organisation or deal context"}

    c = cast(Any, DealMemory).__table__.c

    stmt = select(DealMemory).where(
        c.organisation_id == organisation_id,
        c.deal_id == deal_id,
    )

    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            memory = result.scalar_one_or_none()
        except Exception as e:
            logger.exception(f"get_deal_memory: database error: {e}")
            return {"status": "NOT_AVAILABLE", "message": "Error reading deal memory"}

        if not memory:
            logger.info("get_deal_memory: no memory for deal %s", deal_id)
            return {
                "status": "NOT_AVAILABLE",
                "message": "Deal memory has not been generated yet",
            }

        output: dict[str, Any] = {
            "status": "AVAILABLE",
            "content": memory.content,
            "version": memory.version,
            "token_count": memory.token_count,
        }

    logger.info(
        "get_deal_memory: found memory for deal %s (version=%d, tokens=%s)",
        deal_id,
        memory.version,
        memory.token_count,
    )
    return output


def create_get_deal_memory(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_deal_memory tool bound to a context.

    SECURITY: organisation_id and deal_id come from context (immutable).
    """

    async def get_deal_memory_tool() -> dict[str, Any]:
        """Get the persistent deal memory for the current deal.

        Use this tool FIRST to get accumulated context about a deal before
        gathering additional data. The deal memory contains a synthesised
        overview of all meetings, emails, stakeholders, risks, and business
        narrative — built by prior analysis runs.

        No parameters needed — uses the current deal context.

        Returns a markdown document with:
        - Deal snapshot (stage, amount, dates, data density)
        - Stakeholder map (roles, stance, engagement)
        - Business narrative (why the deal exists)
        - Engagement analysis (momentum, participation patterns)
        - Power dynamics (decision structure, champion strength)
        - Risk landscape (evidence-based risks)
        - Execution path (next steps, open questions)

        Returns NOT_AVAILABLE if no memory has been generated yet.
        """
        logger.info("[TOOL CALLED] get_deal_memory - deal_id='%s'", context.deal_id)

        if not context.organisation_id or not context.deal_id:
            logger.warning("get_deal_memory unavailable: missing context")
            return {"status": "NOT_AVAILABLE", "message": "Missing deal context"}

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetDealMemory",
            tool_func=get_deal_memory,
            parameters={"context": context},
            description="Getting deal memory",
            fallback_value={"status": "NOT_AVAILABLE", "message": "Tool execution failed"},
        )

        logger.info(
            "[TOOL RESULT] get_deal_memory - status: %s",
            result.get("status", "unknown") if result else "None",
        )
        return result

    return get_deal_memory_tool


__all__ = [
    "get_deal_memory",
    "create_get_deal_memory",
]
