"""
Base classes and utilities for agent tools.

This module provides:
- OrgScopedTool: Base validation for organisation-scoped database tools
- Common decorators and helpers for tool creation
- Database session lock for concurrent access protection
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

# Context variable to store per-context lock
# This ensures each async context (event loop) gets its own lock
_db_lock_var: ContextVar[asyncio.Lock | None] = ContextVar("db_session_lock", default=None)


def get_db_session_lock() -> asyncio.Lock:
    """Get or create a database session lock for the current async context.

    This avoids the "bound to different event loop" error by creating
    the lock lazily within the current event loop context.

    Returns:
        asyncio.Lock for the current context
    """
    lock = _db_lock_var.get()
    if lock is None:
        lock = asyncio.Lock()
        _db_lock_var.set(lock)
    return lock


# Keep the old name as an alias for backward compatibility,
# but it's now a function that returns the current context's lock
db_session_lock = get_db_session_lock


def validate_org_context(context: AgentContext | None, require_session: bool = False) -> bool:
    """Validate that context has required org-scoped metadata.

    Args:
        context: AgentContext to validate
        require_session: If True, also require legacy session to be present.
                        Set to False for tools that use context.acquire_session().

    Returns:
        True if context is valid for org-scoped operations
    """
    if not context:
        logger.warning("Tool called without context")
        return False
    if not context.organisation_id:
        logger.warning("Tool called without organisation_id")
        return False
    # Only check session if explicitly required (legacy tools)
    if require_session and not context.session:
        logger.warning("Tool called without database session (legacy mode)")
        return False
    return True


def require_org_context(fallback_value: Any = None) -> Callable[[Callable], Callable]:
    """Decorator that validates org context before executing tool.

    Args:
        fallback_value: Value to return if context validation fails

    Returns:
        Decorated function that validates context
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            context = kwargs.get("context")
            if not validate_org_context(context):
                return fallback_value
            return await func(*args, **kwargs)

        return wrapper

    return decorator


async def execute_with_capabilities(
    capabilities: AgentCapabilities | None,
    *,
    tool_name: str,
    tool_func: Callable,
    parameters: dict[str, Any],
    description: str,
    fallback_value: Any = None,
) -> Any:
    """Execute a tool function with capability tracking.

    Provides transparent tool usage tracking via SSE events when capabilities
    are available, otherwise executes directly.

    Args:
        capabilities: AgentCapabilities for tracking (optional)
        tool_name: Display name for the tool
        tool_func: Async function to execute
        parameters: Parameters to pass to tool_func
        description: Human-readable description of what tool is doing
        fallback_value: Value to return on error

    Returns:
        Tool result or fallback_value on error
    """
    if not capabilities:
        # Execute directly without tracking
        try:
            if asyncio.iscoroutinefunction(tool_func):
                result = await tool_func(**parameters)
            else:
                result = tool_func(**parameters)
            return result if result is not None else fallback_value
        except Exception as exc:
            logger.error(f"{tool_name} failed: {exc}", exc_info=True)
            return fallback_value

    # Execute with capability tracking
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


class ToolResult:
    """Standard result wrapper for tool outputs."""

    @staticmethod
    def empty_list() -> list[dict[str, str]]:
        """Return empty list for search-type tools."""
        return []

    @staticmethod
    def not_found(message: str = "Not found") -> dict[str, Any]:
        """Return not found response."""
        return {"error": "not_found", "message": message}

    @staticmethod
    def error(message: str) -> dict[str, Any]:
        """Return error response."""
        return {"error": "error", "message": message}


__all__ = [
    "db_session_lock",
    "validate_org_context",
    "require_org_context",
    "execute_with_capabilities",
    "ToolResult",
]
