from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.agents.tools.transparent_tools import AgentCapabilities
from src.infra.utils.logger import get_logger

logger = get_logger()


async def use_tool_with_capabilities(
    capabilities: AgentCapabilities | None,
    *,
    tool_name: str,
    tool_func: Callable,
    parameters: dict,
    description: str,
    fallback_value: Any,
):
    """Standard wrapper to call tools through AgentCapabilities."""
    if not capabilities:
        logger.error("Capabilities not initialized")
        return fallback_value

    try:
        result = await capabilities.use_tool(
            tool_name=tool_name,
            tool_func=tool_func,
            parameters=parameters,
            description=description,
        )
        return result if result is not None else fallback_value
    except Exception as exc:
        logger.error(f"{tool_name} tool failed: {exc}", exc_info=True)
        return fallback_value
