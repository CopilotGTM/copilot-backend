"""Create-only stakeholder tool for deal-level agents.

Creates a stakeholder by email when no stakeholder with that email already exists
for the current deal. Existing stakeholders are returned as `already_exists`
without any mutation.

This tool is intended for missing buyer-side stakeholders discovered from deal
context, especially materially discussed people who are relevant to the current
project or buying process. It should not be used for attendee or invite-based
stakeholder mapping, which is handled elsewhere.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.crm.upsert_stakeholder import (
    AUTOMATED_PIPELINE_USER_ID,
    UpsertStakeholderInput,
    _clean_text,
    _dispatch_research_for_stakeholder_create,
    _format_upsert_output,
    _normalize_optional_decision_power,
    _normalize_optional_stance,
    _normalize_optional_temperature,
)
from src.contracts.crm.stakeholders import CreateStakeholderRequest
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DecisionPower,
    StakeholderStance,
    StakeholderTemperature,
)
from src.service.crm.guards.internal_user_guard import is_internal_email
from src.service.crm.insights.stakeholders_service import (
    StakeholdersServiceError,
)
from src.service.crm.participants.contracts import StakeholderWriteContext
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def create_stakeholder_if_missing(
    *,
    context: AgentContext,
    deal_id: str | None = None,
    input_data: UpsertStakeholderInput,
) -> dict[str, Any] | None:
    """Create a stakeholder only when the email is new for the current deal.

    Caller intent:
    - Use for net-new buyer-side stakeholders who are materially relevant to the
      current deal or project.
    - Do not use for existing stakeholders.
    - Do not use for attendee or invite-only people whose meeting mapping is
      handled by a separate pipeline.
    """
    organisation_id = context.organisation_id
    actual_deal_id = UUID(deal_id) if deal_id else context.deal_id

    if not organisation_id or not actual_deal_id:
        return None

    email = input_data.email.strip().lower()
    if not email:
        return None

    if await is_internal_email(context, email):
        return {
            "email": email,
            "action": "rejected_internal_user",
            "updated": False,
            "created": False,
            "updated_fields": [],
            "message": (
                "This email belongs to an internal team member "
                "and cannot be added as a stakeholder."
            ),
        }

    async with context.acquire_session() as session:
        try:
            service = StakeholderWriteService(
                session,
                organisation_id,
                user_id=context.user_id or AUTOMATED_PIPELINE_USER_ID,
            )

            async def _already_checked_internal_email(email: str) -> bool:
                _ = email
                return False

            service._is_internal_email = _already_checked_internal_email  # type: ignore[method-assign]
            stakeholder = await service._find_contact_participant(
                deal_id=actual_deal_id,
                email=email,
                include_deleted=True,
            )

            if stakeholder:
                result = _format_upsert_output(
                    stakeholder,
                    action="already_exists",
                    updated_fields=[],
                )
                result["action"] = "already_exists"
                result["message"] = "Stakeholder already exists; no changes applied"
                return result

            try:
                new_stakeholder = await service.create_stakeholder(
                    actual_deal_id,
                    CreateStakeholderRequest(
                        email=email,
                        name=_clean_text(input_data.name),
                        job_title=_clean_text(input_data.job_title),
                        phone_number=_clean_text(input_data.phone_number),
                        department=_clean_text(input_data.department),
                        stance=input_data.stance or StakeholderStance.UNKNOWN,
                        decision_power=input_data.decision_power or DecisionPower.UNKNOWN,
                        motivation=_clean_text(input_data.motivation),
                        pain_points=input_data.pain_points or None,
                    ),
                    context=StakeholderWriteContext(
                        source="agent",
                        actor_user_id=context.user_id,
                        source_label="Agent",
                        created_via="create_stakeholder_if_missing_agent",
                    ),
                )
            except StakeholdersServiceError:
                logger.exception("create_stakeholder_if_missing: service create failed")
                return None

            await session.commit()

            # Dispatch stakeholder research (fire-and-forget)
            try:
                await _dispatch_research_for_stakeholder_create(
                    organisation_id=organisation_id,
                    stakeholder=new_stakeholder,
                    deal_id=actual_deal_id,
                    user_id=context.user_id,
                )
            except Exception:
                logger.warning(
                    "Failed to dispatch research for %s",
                    new_stakeholder.participant_id,
                    exc_info=True,
                )

            result = _format_upsert_output(
                new_stakeholder,
                action="created",
                updated_fields=[],
            )
            result["action"] = "created"
            return result
        except Exception:
            await session.rollback()
            logger.exception("create_stakeholder_if_missing: database error")
            return None


def create_create_stakeholder_if_missing(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the create-only stakeholder tool bound to a context."""

    async def create_stakeholder_if_missing_tool(
        email: str,
        deal_id: str | None = None,
        name: str | None = None,
        job_title: str | None = None,
        phone_number: str | None = None,
        department: str | None = None,
        stance: StakeholderStance | str | None = None,
        decision_power: DecisionPower | str | None = None,
        temperature: StakeholderTemperature | str | None = None,
        motivation: str | None = None,
        pain_points: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Create a stakeholder only when the email is not already present on the deal.

        This tool is intentionally create-only for workflows such as meeting
        summary generation, where existing stakeholders should be read and matched
        but not updated.

        Use this only for missing buyer-side people who were materially discussed
        and are relevant to the current deal or project. Do not call it for:
        - attendee-roster or invite-only people
        - people who already exist in the stakeholder list
        - weakly identified names
        - people whose affiliation to the buyer/prospect organization is not reliable

        If the email already exists, the tool returns `already_exists` and leaves
        the record unchanged.
        """
        logger.info("[TOOL CALLED] create_stakeholder_if_missing - email='%s'", email)

        if not context.organisation_id:
            logger.warning("create_stakeholder_if_missing unavailable: missing organisation_id")
            return None

        effective_deal_id = deal_id or (str(context.deal_id) if context.deal_id else None)
        if not effective_deal_id:
            logger.warning(
                "create_stakeholder_if_missing unavailable: no deal_id provided or in context"
            )
            return None

        input_data = UpsertStakeholderInput(
            email=email,
            name=name,
            job_title=job_title,
            phone_number=phone_number,
            department=department,
            stance=_normalize_optional_stance(stance),
            decision_power=_normalize_optional_decision_power(decision_power),
            temperature=_normalize_optional_temperature(temperature),
            motivation=motivation,
            pain_points=pain_points,
            only_fill_missing=True,
            create_if_missing=True,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateStakeholderIfMissing",
            tool_func=create_stakeholder_if_missing,
            parameters={"context": context, "deal_id": effective_deal_id, "input_data": input_data},
            description="Creating a missing, discussed buyer-side stakeholder if not already present",
            fallback_value=None,
        )

        if result:
            action = result.get("action")
            if action == "rejected_internal_user":
                logger.info(
                    "[TOOL RESULT] create_stakeholder_if_missing rejected internal email=%s",
                    result.get("email"),
                )
            else:
                logger.info(
                    "[TOOL RESULT] create_stakeholder_if_missing action=%s email=%s",
                    action,
                    result.get("email"),
                )
        else:
            logger.info("[TOOL RESULT] create_stakeholder_if_missing failed")

        return dict(result) if result else None

    return create_stakeholder_if_missing_tool


__all__ = [
    "create_stakeholder_if_missing",
    "create_create_stakeholder_if_missing",
]
