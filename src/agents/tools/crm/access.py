"""Authorization helpers shared by human-facing CRM agent tools.

Agent tools receive immutable organisation and user IDs from ``AgentContext``.
These helpers resolve the master-user role once per tool call and enforce the
same deal policy used by the HTTP API before any deal content is returned.
"""

from __future__ import annotations

from uuid import UUID

from src.agents.context import AgentContext
from src.domain.models.master_tables import User
from src.infra.database.sessions import acquire_master_async_session
from src.service.crm.access.record_access_policy import (
    CRMAccessActor,
    CRMActor,
    CRMRecordAccessError,
    CRMSystemActor,
    DealAccessLevel,
    DealAccessPolicy,
)


class AgentCRMAccessError(Exception):
    """Raised when an agent run lacks a human identity or deal permission."""


async def resolve_agent_access_actor(context: AgentContext) -> CRMActor:
    """Resolve the authenticated app user represented by an agent invocation."""
    if context.user_id is None:
        system_source = context.metadata.get("crm_system_actor")
        if isinstance(system_source, str) and system_source.strip():
            return CRMSystemActor(system_source.strip())
        if context.deal_id is not None:
            # Scheduled analysis agents are born with an immutable deal scope.
            # The per-deal guard below prevents this principal from escaping it.
            return CRMSystemActor("deal_scoped_agent")
        raise AgentCRMAccessError("CRM access requires an authenticated user")

    # Agent jobs carry immutable user IDs but not roles; resolve the role from
    # the master record so an admin keeps admin scope without trusting prompts.
    async for session in acquire_master_async_session():
        user = await session.get(User, context.user_id)
        if user and user.organisation_id == context.organisation_id:
            return CRMAccessActor(
                user.user_id, user.user_role, str(user.email) if user.email else None
            )
        break
    raise AgentCRMAccessError("CRM access requires an active organisation user")


async def require_agent_deal_access(
    context: AgentContext,
    session,
    deal_id: UUID,
    level: DealAccessLevel = "view",
) -> CRMActor:
    """Require a human agent caller to have the requested parent-deal access."""
    actor = await resolve_agent_access_actor(context)
    if isinstance(actor, CRMSystemActor) and context.deal_id != deal_id:
        raise AgentCRMAccessError("System workflow is bound to a different deal")
    try:
        await DealAccessPolicy(session, context.organisation_id, actor).require_deal(deal_id, level)
    except CRMRecordAccessError as exc:
        raise AgentCRMAccessError(exc.message) from exc
    return actor
