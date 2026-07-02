"""Agent tool used by chat workflows to add CRM stakeholders.

The tool creates the stakeholder immediately once a resolved `deal_id` and an
explicit stakeholder name, email, or LinkedIn URL are available.

The tool is invoked with an immutable `organisation_id` context from the agent
runtime, which prevents cross-org writes and keeps stakeholder mutation logic in
the CRM service boundary.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access

This is an ORG-SCOPED tool. deal_id is NOT required at init.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.contracts.crm.stakeholders import CreateStakeholderRequest
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DecisionPower,
    StakeholderStance,
)
from src.service.crm.insights.stakeholders_service import (
    StakeholdersServiceError,
)
from src.service.crm.jobs.stakeholder_job_visibility_service import (
    StakeholderJobVisibilityService,
)
from src.service.crm.participants.contracts import StakeholderWriteContext
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _clean_optional(value: str | None) -> str | None:
    """Normalize optional user input while preserving explicit empty submissions."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _enum_value(value: Any) -> str | None:
    """Convert enum-like values to string in a safe, API-friendly format."""
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _parse_stance(value: str | None) -> StakeholderStance | None | dict[str, Any]:
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    try:
        return StakeholderStance(cleaned.upper())
    except ValueError:
        allowed = ", ".join(item.value for item in StakeholderStance)
        return {
            "status": "error",
            "message": f"Invalid stance '{cleaned}'. Use one of: {allowed}.",
        }


def _parse_decision_power(value: str | None) -> DecisionPower | None | dict[str, Any]:
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    try:
        return DecisionPower(cleaned.upper())
    except ValueError:
        allowed = ", ".join(item.value for item in DecisionPower)
        return {
            "status": "error",
            "message": f"Invalid decision_power '{cleaned}'. Use one of: {allowed}.",
        }


def _job_summary(job: Any) -> dict[str, str]:
    return {
        "job_id": str(job.job_id),
        "job_type": _enum_value(job.job_type) or "",
        "status": _enum_value(job.status) or "",
    }


def _research_job_summary(
    job_id: UUID | str | None,
    *,
    job_type: str = "contact_research",
) -> dict[str, str | None] | None:
    if not job_id:
        return None
    return {
        "job_id": str(job_id),
        "job_type": job_type,
        "status": "pending",
    }


def _post_create_status(
    *,
    queued: bool,
    error: str | None = None,
) -> dict[str, str]:
    status = "failed" if error else "queued" if queued else "not_created"
    summary = {"status": status}
    if error:
        summary["error"] = error
    return summary


def _stakeholder_name(stakeholder: Any) -> str:
    return str(stakeholder.name or stakeholder.email or stakeholder.participant_id)


def _has_rematch_identity(stakeholder: Any) -> bool:
    return bool(getattr(stakeholder, "email", None))


def _stakeholder_contact_id(stakeholder: Any) -> UUID | None:
    contact_id = getattr(stakeholder, "contact_id", None)
    return contact_id if isinstance(contact_id, UUID) else None


def _created_stakeholder_result(
    *,
    deal_id: UUID,
    deal_name: str,
    stakeholder: Any,
    processing_jobs: list[Any],
    research_job_id: UUID | str | None,
    research_job_type: str = "contact_research",
    processing_error: str | None = None,
    research_error: str | None = None,
) -> dict[str, Any]:
    processing_job_summaries = [_job_summary(job) for job in processing_jobs]
    result: dict[str, Any] = {
        "status": "created",
        "participant_id": str(stakeholder.participant_id),
        "deal_id": str(deal_id),
        "deal_name": deal_name,
        "email": stakeholder.email,
        "name": stakeholder.name,
        "job_title": getattr(stakeholder, "job_title", None),
        "phone_number": getattr(stakeholder, "phone_number", None),
        "stance": _enum_value(getattr(stakeholder, "stance", None)),
        "decision_power": _enum_value(getattr(stakeholder, "decision_power", None)),
        "approval_status": _enum_value(getattr(stakeholder, "approval_status", None)),
        "processing_jobs": processing_job_summaries,
        "research_job": _research_job_summary(research_job_id, job_type=research_job_type),
        "post_create_jobs": {
            "processing": _post_create_status(
                queued=bool(processing_jobs),
                error=processing_error,
            ),
            "research": _post_create_status(
                queued=research_job_id is not None,
                error=research_error,
            ),
        },
        "message": (
            f"Added **{_stakeholder_name(stakeholder)}** as a stakeholder to **{deal_name}**."
        ),
    }

    return result


async def add_stakeholder_direct(
    *,
    context: AgentContext,
    deal_id: str,
    deal_name: str,
    email: str | None = None,
    linkedin_url: str | None = None,
    name: str | None = None,
    job_title: str | None = None,
    phone_number: str | None = None,
    department: str | None = None,
    stance: str | None = None,
    decision_power: str | None = None,
    motivation: str | None = None,
    pain_points: list[str] | None = None,
) -> dict[str, Any]:
    """Create a stakeholder directly through the CRM stakeholder service."""
    # Security boundary check is deliberately early: the tool cannot execute for
    # missing org/user context because those IDs are the only safe tenant filter.
    organisation_id = context.organisation_id
    user_id = context.user_id
    clean_deal_name = _clean_optional(deal_name)
    clean_email = _clean_optional(email)
    clean_linkedin_url = _clean_optional(linkedin_url)
    clean_name = _clean_optional(name)

    if not organisation_id:
        logger.warning("add_stakeholder_direct: missing organisation_id in context")
        return {
            "status": "error",
            "message": "Cannot add stakeholder: missing organisation context.",
        }
    if not user_id:
        logger.warning("add_stakeholder_direct: missing user_id in context")
        return {"status": "error", "message": "Could not identify your account. Please sign in."}
    if not clean_deal_name:
        return {"status": "error", "message": "Deal name is required before adding a stakeholder."}
    if not any((clean_email, clean_linkedin_url, clean_name)):
        return {
            "status": "error",
            "message": "Stakeholder name, email, or LinkedIn URL is required.",
        }

    try:
        deal_uuid = UUID(deal_id)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "message": "Invalid deal information. Please search the deal again.",
        }

    parsed_stance = _parse_stance(stance)
    if isinstance(parsed_stance, dict):
        return parsed_stance
    parsed_decision_power = _parse_decision_power(decision_power)
    if isinstance(parsed_decision_power, dict):
        return parsed_decision_power

    try:
        request = CreateStakeholderRequest(
            email=clean_email,
            linkedin_url=clean_linkedin_url,
            name=clean_name,
            job_title=_clean_optional(job_title),
            phone_number=_clean_optional(phone_number),
            department=_clean_optional(department),
            stance=parsed_stance or StakeholderStance.UNKNOWN,
            decision_power=parsed_decision_power or DecisionPower.UNKNOWN,
            motivation=_clean_optional(motivation),
            pain_points=pain_points,
        )
    except ValueError as exc:
        return {"status": "error", "message": f"Invalid stakeholder details: {exc}"}

    # Keep DB session scope tight to this path; all writes stay in-service and
    # can fail independently from the outbound research dispatch.
    async with context.acquire_session() as session:
        service = StakeholderWriteService(session, organisation_id, user_id)
        try:
            stakeholder = await service.create_stakeholder(
                deal_uuid,
                request,
                context=StakeholderWriteContext(
                    source="agent",
                    actor_user_id=user_id,
                    source_label="Agent",
                    created_via="add_stakeholder_agent",
                ),
            )
            await session.commit()
            processing_jobs: list[Any] = []
            processing_error = None
            research_job_id = None
            research_job_type = "contact_research"
            research_error = None

            if _has_rematch_identity(stakeholder):
                try:
                    job_visibility_service = StakeholderJobVisibilityService(
                        session,
                        organisation_id,
                        user_id,
                    )
                    processing_jobs = (
                        await job_visibility_service.create_stakeholder_processing_jobs(
                            deal_uuid,
                            stakeholder.participant_id,
                        )
                    )
                except Exception as exc:
                    processing_error = str(exc)
                    logger.exception(
                        "add_stakeholder_direct: stakeholder created but processing dispatch failed"
                    )

            try:
                from src.workers.deal_worker.utils.contact_research_dispatch import (
                    dispatch_contact_research_for_contact,
                )

                research_job_id = await dispatch_contact_research_for_contact(
                    organisation_id=organisation_id,
                    contact_id=_stakeholder_contact_id(stakeholder),
                    user_id=user_id,
                    trigger="stakeholder_created",
                )
                research_job_type = "contact_research"
            except Exception as exc:
                research_error = str(exc)
                logger.exception(
                    "add_stakeholder_direct: stakeholder created but contact research dispatch failed"
                )

            return _created_stakeholder_result(
                deal_id=deal_uuid,
                deal_name=clean_deal_name,
                stakeholder=stakeholder,
                processing_jobs=processing_jobs,
                research_job_id=research_job_id,
                research_job_type=research_job_type,
                processing_error=processing_error,
                research_error=research_error,
            )
        except StakeholdersServiceError as exc:
            logger.warning("add_stakeholder_direct: StakeholdersServiceError: %s", exc.message)
            result: dict[str, Any] = {
                "status": "error",
                "message": f"Could not add stakeholder: {exc.message}",
            }
            if exc.data:
                result["conflict"] = exc.data
            return result
        except Exception:
            logger.exception("add_stakeholder_direct: unexpected error")
            return {
                "status": "error",
                "message": "An unexpected error occurred while adding the stakeholder.",
            }


def create_add_stakeholder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the add_stakeholder tool bound to a context.

    SECURITY: organisation_id comes from context (immutable, not a tool parameter).
    """

    async def add_stakeholder_tool(
        deal_id: str,
        deal_name: str,
        email: str | None = None,
        linkedin_url: str | None = None,
        name: str | None = None,
        job_title: str | None = None,
        phone_number: str | None = None,
        department: str | None = None,
        stance: str | None = None,
        decision_power: str | None = None,
        motivation: str | None = None,
        pain_points: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Create a stakeholder directly on an existing deal.

        IMPORTANT: Only add EXTERNAL contacts (clients, prospects, partners) as
        stakeholders. Do NOT add internal team members or anyone from your own
        organisation. Internal emails are rejected automatically by the service.

        IMPORTANT: Before calling this tool, you MUST first use search_deals to
        find the deal. Do NOT call this tool without a resolved deal_id.

        Provide any one of email, linkedin_url, or name. Email is not
        required for a placeholder stakeholder.

        Workflow:
        1. User asks to add a stakeholder to a deal
        2. You call search_deals to find matching deals
        3. If multiple matches, ask user which deal they mean
        4. Once the deal and email are known, call this tool with any optional
           stakeholder details the user mentioned

        Args:
            deal_id: UUID of the deal (from search_deals result)
            deal_name: Name of the deal (from search_deals result)
            email: Stakeholder email address
            linkedin_url: Stakeholder LinkedIn URL
            name: Optional stakeholder name
            job_title: Optional job title
            phone_number: Optional phone number
            department: Optional department
            stance: Optional stance (UNKNOWN/CHAMPION/SUPPORTER/
                    NEUTRAL/DETRACTOR/BLOCKER)
            decision_power: Optional decision power (UNKNOWN/
                           DECISION_MAKER/INFLUENCER/EVALUATOR/GATEKEEPER/END_USER)
            motivation: Optional motivation/context
            pain_points: Optional list of pain points

        Returns:
            Structured status dict with created stakeholder details or an error.
        """
        logger.info(
            "[TOOL CALLED] add_stakeholder - deal_id='%s', deal_name='%s', email=%s",
            deal_id,
            deal_name,
            email,
        )

        if not context.organisation_id:
            logger.warning("add_stakeholder unavailable: missing organisation_id")
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="AddStakeholder",
            tool_func=add_stakeholder_direct,
            parameters={
                "context": context,
                "deal_id": deal_id,
                "deal_name": deal_name,
                "email": email,
                "linkedin_url": linkedin_url,
                "name": name,
                "job_title": job_title,
                "phone_number": phone_number,
                "department": department,
                "stance": stance,
                "decision_power": decision_power,
                "motivation": motivation,
                "pain_points": pain_points,
            },
            description="Adding stakeholder",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] add_stakeholder status=%s",
                result.get("status"),
            )
        else:
            logger.info("[TOOL RESULT] add_stakeholder returned None (error)")

        return dict(result) if result else None

    return add_stakeholder_tool


__all__ = [
    "add_stakeholder_direct",
    "create_add_stakeholder",
]
