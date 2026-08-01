"""Search the organisation's canonical CRM user directory."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.agents.tools.base import execute_with_capabilities
from src.service.crm.access.crm_user_directory_service import (
    MAX_CRM_USER_QUERY_CHARS,
    CRMUserDirectoryService,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def search_crm_users(*, context: AgentContext, query: str) -> dict[str, Any]:
    """Find an active team member by CRM user ID, name, or email."""
    if not query.strip() or len(query.strip()) > MAX_CRM_USER_QUERY_CHARS:
        return {"status": "not_found", "candidates": []}

    async with context.acquire_session() as session:
        result = await CRMUserDirectoryService(
            session,
            context.organisation_id,
        ).search(query)

    return {
        "status": result.status,
        "match_type": result.match_type,
        "candidates": [
            {
                "user_id": str(user.user_id),
                "name": user.full_name
                or " ".join(part for part in (user.first_name, user.last_name) if part)
                or None,
                "email": user.email,
            }
            for user in result.candidates
        ],
    }


def create_search_crm_users(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Bind CRM-user lookup to the immutable agent organisation."""

    async def search_crm_users_tool(query: str) -> dict[str, Any]:
        """Find a person from our team by name, email, or CRM user ID.

        Use this before filtering deals by a named owner, analyzing a team
        member, or resolving phrases such as "our rep" and "from our team".
        Do not guess an email address from a name. A fuzzy_email result is a
        suggestion even when only one candidate is returned, so repeat the
        user's address and ask before using the candidate. If status is ambiguous, ask
        the user to choose from the returned candidates. If a name is not
        found, ask for an email; if that direct identifier is also not found,
        report no match. An error means the lookup failed, not that the person
        is absent, so ask the user to try again.
        """
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="SearchCRMUsers",
            tool_func=search_crm_users,
            parameters={"context": context, "query": query},
            description="Searching team members",
            fallback_value={
                "status": "error",
                "message": "Could not search team members. Please try again.",
                "candidates": [],
            },
        )
        return (
            dict(result)
            if result
            else {
                "status": "error",
                "message": "Could not search team members. Please try again.",
                "candidates": [],
            }
        )

    return search_crm_users_tool


__all__ = ["create_search_crm_users", "search_crm_users"]
