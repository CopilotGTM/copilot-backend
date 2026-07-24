"""List and filter deal stakeholders for agents.

Tool for reading stakeholders associated with a deal. It intentionally covers
both "show the buying committee" and "show me this one person" so agents do not
need a second stakeholder-detail tool for the same data shape.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Always from context — prevents cross-org data access

deal_id can be passed as an optional parameter. If not provided, falls back
to context.deal_id. This allows the tool to be used both in deal-scoped
views and in cross-deal queries (e.g. from search_deals results).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.access import AgentCRMAccessError, require_agent_deal_access
from src.service.crm.participants.stakeholder_read_service import StakeholderReadService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


async def list_deal_stakeholders(
    *,
    context: AgentContext,
    deal_id: str | None = None,
    stakeholder_id: str | None = None,
    query: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List stakeholders for a deal, optionally narrowed to one stakeholder.

    Agents use this as the single stakeholder read tool. Passing
    `stakeholder_id` returns at most one row; passing `query` filters the deal's
    stakeholders by visible person fields such as name, email, title, and
    department. Person profile and research fields are contact-owned; this tool
    reads them from the linked contact when one exists and keeps stakeholder
    fields for deal-specific context such as stance, decision power, motivation,
    and pain points. The response shape stays list-based so the model has one
    pattern for broad and narrow stakeholder questions.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access

    Args:
        context: AgentContext containing organisation and deal metadata
        stakeholder_id: Optional participant UUID to return as a one-item list
        query: Optional case-insensitive filter for name/email/title/department
        limit: Maximum number of stakeholders to return (default: 20)

    Returns:
        List of stakeholder dictionaries with summary information.
    """
    # SECURITY: organisation_id always from context (immutable)
    organisation_id = context.organisation_id
    # deal_id: parameter overrides context
    actual_deal_id = UUID(deal_id) if deal_id else context.deal_id

    if not organisation_id:
        logger.warning("list_deal_stakeholders: missing organisation_id in context")
        return []

    if not actual_deal_id:
        logger.warning("list_deal_stakeholders: no deal_id provided and none in context")
        return []

    effective_limit = max(1, min(limit, 50))
    parsed_stakeholder_id: UUID | None = None
    if stakeholder_id:
        try:
            parsed_stakeholder_id = UUID(stakeholder_id)
        except ValueError:
            logger.warning(
                "list_deal_stakeholders: invalid stakeholder_id format: %s",
                stakeholder_id,
            )
            return []

    # Acquire fresh session for database operations
    async with context.acquire_session() as session:
        try:
            actor = await require_agent_deal_access(context, session, actual_deal_id)
            service = StakeholderReadService(
                session,
                organisation_id,
                getattr(context, "user_id", None),
                access_actor=actor,
            )
            if parsed_stakeholder_id:
                payload = await service.get_stakeholder_detail(
                    actual_deal_id,
                    parsed_stakeholder_id,
                )
                stakeholders = [payload] if payload else []
            else:
                result = await service.list_stakeholders_by_deal(
                    actual_deal_id,
                    include_pending=True,
                    include_rejected=False,
                    limit=50 if query else effective_limit,
                )
                stakeholders = result.stakeholders
        except AgentCRMAccessError:
            logger.info("list_deal_stakeholders: deal not visible to agent actor")
            return []
        except Exception as e:
            logger.exception(f"list_deal_stakeholders: database error: {e}")
            return []

        output: list[dict[str, Any]] = []
        for stakeholder in stakeholders:
            payload = _stakeholder_payload(stakeholder)
            if query and not _matches_query(payload, query):
                continue
            output.append(
                {
                    "participant_id": str(payload["participant_id"]),
                    "deal_id": str(payload["deal_id"]),
                    "contact_id": str(payload["contact_id"]) if payload["contact_id"] else None,
                    "email": payload["email"],
                    "name": payload["name"],
                    "job_title": payload["job_title"],
                    "relationship_role": payload["relationship_role"],
                    "phone_number": payload["phone_number"],
                    "department": payload["department"],
                    "linkedin_url": payload["linkedin_url"],
                    "external_links": _external_links_payload(payload["external_links"]),
                    "stance": _enum_value(payload["stance"]),
                    "decision_power": _enum_value(payload["decision_power"]),
                    "motivation": payload["motivation"],
                    "pain_points": payload["pain_points"] or [],
                    "keywords": payload["keywords"] or [],
                    "temperature": _enum_value(payload["temperature"]),
                    "last_contact_at": payload["last_contact_at"].isoformat()
                    if payload["last_contact_at"]
                    else None,
                    "source": _enum_value(payload["source"]),
                    "stakeholder_research": payload["stakeholder_research"],
                    "last_researched_at": payload["last_researched_at"].isoformat()
                    if payload["last_researched_at"]
                    else None,
                }
            )
            if len(output) >= effective_limit:
                break
    # Session released

    logger.info(
        "list_deal_stakeholders: found %d stakeholders for deal %s", len(output), actual_deal_id
    )
    return output


def _stakeholder_payload(stakeholder: Any) -> dict[str, Any]:
    """Normalize service DTOs and detail dictionaries to one agent payload."""
    if isinstance(stakeholder, dict):
        return stakeholder
    return {
        "participant_id": stakeholder.participant_id,
        "deal_id": stakeholder.deal_id,
        "contact_id": getattr(stakeholder, "contact_id", None),
        "email": stakeholder.email,
        "name": stakeholder.name,
        "job_title": stakeholder.job_title,
        "relationship_role": getattr(stakeholder, "relationship_role", None),
        "phone_number": stakeholder.phone_number,
        "department": getattr(stakeholder, "department", None),
        "linkedin_url": getattr(stakeholder, "linkedin_url", None),
        "external_links": getattr(stakeholder, "external_links", None),
        "stance": stakeholder.stance,
        "decision_power": stakeholder.decision_power,
        "motivation": getattr(stakeholder, "motivation", None),
        "pain_points": getattr(stakeholder, "pain_points", None),
        "keywords": getattr(stakeholder, "keywords", None),
        "temperature": stakeholder.temperature,
        "last_contact_at": stakeholder.last_contact_at,
        "source": stakeholder.source,
        "stakeholder_research": getattr(stakeholder, "stakeholder_research", None),
        "last_researched_at": getattr(stakeholder, "last_researched_at", None),
    }


def _external_links_payload(value: Any) -> list[dict[str, Any]]:
    """Return external links as plain dicts for agent tool responses."""
    links = value or []
    output: list[dict[str, Any]] = []
    for link in links:
        if hasattr(link, "model_dump"):
            raw = link.model_dump(exclude_none=True)
        elif isinstance(link, dict):
            raw = {key: item for key, item in link.items() if item is not None}
        else:
            continue
        output.append(
            {
                key: raw[key]
                for key in ("platform", "url", "label")
                if key in raw and raw[key] is not None
            }
        )
    return output


def _matches_query(payload: dict[str, Any], query: str) -> bool:
    """Return whether a stakeholder row matches a user-provided person query."""
    needle = query.strip().lower()
    if not needle:
        return True
    fields = (
        payload.get("name"),
        payload.get("email"),
        payload.get("job_title"),
        payload.get("relationship_role"),
        payload.get("department"),
    )
    return any(needle in str(value).lower() for value in fields if value)


def create_list_deal_stakeholders(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_stakeholders tool bound to a context.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Always from context — prevents cross-org data access

    deal_id can be passed as an optional parameter. If not provided, falls back
    to context.deal_id.
    """

    async def list_deal_stakeholders_tool(
        deal_id: str | None = None,
        stakeholder_id: str | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List or find stakeholders (contacts) associated with a deal.

        Use this tool to understand the buying committee and key people involved
        in the deal. For a single person, pass `stakeholder_id` when you already
        have the participant ID, or `query` when the user names a stakeholder.
        Treat name, email, title, phone, LinkedIn, department, and research as
        linked-contact data; use stakeholder fields only for deal context.

        Args:
            deal_id: Deal UUID to query. If not provided, uses the current deal
                context. Pass this when you got a deal_id from search_deals but
                are not in a deal view.
            stakeholder_id: Optional participant UUID. Returns at most one row.
            query: Optional person search across stakeholder name/email/title/role/department.
            limit: Maximum number of results (default: 20, max: 50)

        Note: organisation_id is automatically set from context and cannot be
        overridden.
        """
        logger.info("[TOOL CALLED] list_deal_stakeholders - limit=%d", limit)

        if not context.organisation_id:
            logger.warning("list_deal_stakeholders unavailable: missing organisation_id in context")
            return []

        effective_deal_id = deal_id or (str(context.deal_id) if context.deal_id else None)
        if not effective_deal_id:
            logger.warning("list_deal_stakeholders unavailable: no deal_id provided or in context")
            return []

        # Tool acquires its own sessions on-demand, no lock needed
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ListDealStakeholders",
            tool_func=list_deal_stakeholders,
            parameters={
                "context": context,
                "deal_id": effective_deal_id,
                "stakeholder_id": stakeholder_id,
                "query": query,
                "limit": limit,
            },
            description="Looking up stakeholders",
            fallback_value=[],
        )

        logger.info(
            "[TOOL RESULT] list_deal_stakeholders returned %d stakeholders",
            len(result) if result else 0,
        )
        return list(result) if result else []

    return list_deal_stakeholders_tool
