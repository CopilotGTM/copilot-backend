"""Upsert stakeholder tool for deal-level agents.

Single agent-facing stakeholder write tool that updates by email and creates when missing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field

from src.agents.tools.base import execute_with_capabilities
from src.contracts.crm.stakeholders import CreateStakeholderRequest
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)
from src.service.crm.guards.internal_user_guard import is_internal_email
from src.service.crm.insights.stakeholders_service import (
    StakeholdersService,
    StakeholdersServiceError,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


class UpsertStakeholderInput(BaseModel):
    email: Annotated[str, Field(description="Stakeholder email (required)")]
    name: Annotated[str | None, Field(default=None, description="Full name")] = None
    job_title: Annotated[str | None, Field(default=None, description="Job title")] = None
    phone_number: Annotated[str | None, Field(default=None, description="Phone number")] = None
    department: Annotated[str | None, Field(default=None, description="Department")] = None
    stance: Annotated[StakeholderStance | None, Field(default=None)] = None
    decision_power: Annotated[DecisionPower | None, Field(default=None)] = None
    temperature: Annotated[StakeholderTemperature | None, Field(default=None)] = None
    motivation: Annotated[str | None, Field(default=None, description="Motivation")] = None
    pain_points: Annotated[list[str] | None, Field(default=None, description="Pain points")] = None
    only_fill_missing: Annotated[bool, Field(default=True)] = True
    create_if_missing: Annotated[bool, Field(default=True)] = True


def _extract_enum_token(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip().upper()
        return normalized or None

    if isinstance(value, dict):
        for key in ("value", "enum", "name", "label"):
            raw = value.get(key)
            if isinstance(raw, str):
                normalized = raw.strip().upper()
                if normalized:
                    return normalized
        return None

    return None


def _normalize_optional_stance(value: Any) -> StakeholderStance | None:
    token = _extract_enum_token(value)
    if token is None:
        return None
    try:
        return StakeholderStance(token)
    except ValueError:
        return None


def _normalize_optional_decision_power(value: Any) -> DecisionPower | None:
    token = _extract_enum_token(value)
    if token is None:
        return None
    try:
        return DecisionPower(token)
    except ValueError:
        return None


def _normalize_optional_temperature(value: Any) -> StakeholderTemperature | None:
    token = _extract_enum_token(value)
    if token is None:
        return None
    try:
        return StakeholderTemperature(token)
    except ValueError:
        return None


def _with_action(result: dict[str, Any], action: str) -> dict[str, Any]:
    merged = dict(result)
    merged["action"] = action
    return merged


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _is_missing_text(value: str | None) -> bool:
    return value is None or not value.strip()


def _is_unknown_enum(value: Any) -> bool:
    if value is None:
        return True
    raw = value.value if hasattr(value, "value") else value
    return str(raw).upper() == "UNKNOWN"


def _format_upsert_output(
    stakeholder: Any,
    *,
    action: str,
    updated_fields: list[str] | None = None,
) -> dict[str, Any]:
    updates = updated_fields or []
    participant_id = getattr(stakeholder, "participant_id", None) or stakeholder.stakeholder_id
    return {
        "participant_id": str(participant_id),
        "email": getattr(stakeholder, "email", None) or getattr(stakeholder, "related_email", None),
        "name": getattr(stakeholder, "name", None) or getattr(stakeholder, "related_name", None),
        "job_title": stakeholder.job_title,
        "phone_number": stakeholder.phone_number,
        "department": getattr(stakeholder, "department", None),
        "stance": stakeholder.stance.value
        if isinstance(stakeholder.stance, StakeholderStance)
        else stakeholder.stance,
        "decision_power": stakeholder.decision_power.value
        if isinstance(stakeholder.decision_power, DecisionPower)
        else stakeholder.decision_power,
        "temperature": stakeholder.temperature.value
        if isinstance(stakeholder.temperature, StakeholderTemperature)
        else stakeholder.temperature,
        "motivation": stakeholder.motivation,
        "pain_points": stakeholder.pain_points,
        "updated_fields": updates,
        "updated": bool(updates),
        "created": action == "created",
        "message": "Stakeholder created successfully"
        if action == "created"
        else "Stakeholder updated successfully"
        if updates
        else "No changes applied to stakeholder",
    }


async def upsert_stakeholder(
    *,
    context: AgentContext,
    deal_id: str | None = None,
    input_data: UpsertStakeholderInput,
) -> dict[str, Any] | None:
    """Upsert stakeholder by email in the current deal."""
    organisation_id = context.organisation_id
    # deal_id: parameter overrides context
    actual_deal_id = UUID(deal_id) if deal_id else context.deal_id

    if not organisation_id or not actual_deal_id:
        return None

    email = input_data.email.strip().lower()
    if not email:
        return None

    # Guardrail: reject internal team members
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
            service = StakeholdersService(
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
                # If previously removed/rejected/deleted, reactivate first
                if (
                    stakeholder.approval_status
                    in (ApprovalStatus.REMOVED.value, ApprovalStatus.REJECTED.value)
                    or stakeholder.is_deleted
                ):
                    stakeholder.approval_status = ApprovalStatus.APPROVED.value
                    stakeholder.is_deleted = False
                    stakeholder.participant_source = StakeholderSource.MANUAL.value
                    # Reset research so it gets re-researched
                    stakeholder.stakeholder_research = None
                    stakeholder.last_researched_at = None

                updated_fields: list[str] = []
                only_fill_missing = input_data.only_fill_missing

                cleaned_name = _clean_text(input_data.name)
                if cleaned_name is not None and (
                    not only_fill_missing or _is_missing_text(stakeholder.related_name)
                ):
                    stakeholder.related_name = cleaned_name
                    updated_fields.append("name")

                cleaned_job_title = _clean_text(input_data.job_title)
                if cleaned_job_title is not None and (
                    not only_fill_missing or _is_missing_text(stakeholder.job_title)
                ):
                    stakeholder.job_title = cleaned_job_title
                    updated_fields.append("job_title")

                cleaned_department = _clean_text(input_data.department)
                if cleaned_department is not None and (
                    not only_fill_missing or _is_missing_text(stakeholder.department)
                ):
                    stakeholder.department = cleaned_department
                    updated_fields.append("department")

                cleaned_phone = _clean_text(input_data.phone_number)
                if cleaned_phone is not None and (
                    not only_fill_missing or _is_missing_text(stakeholder.phone_number)
                ):
                    stakeholder.phone_number = cleaned_phone
                    updated_fields.append("phone_number")

                if input_data.stance is not None and (
                    not only_fill_missing or _is_unknown_enum(stakeholder.stance)
                ):
                    stakeholder.stance = input_data.stance.value
                    updated_fields.append("stance")

                if input_data.decision_power is not None and (
                    not only_fill_missing or _is_unknown_enum(stakeholder.decision_power)
                ):
                    stakeholder.decision_power = input_data.decision_power.value
                    updated_fields.append("decision_power")

                if input_data.temperature is not None and (
                    not only_fill_missing or _is_unknown_enum(stakeholder.temperature)
                ):
                    stakeholder.temperature = input_data.temperature.value
                    updated_fields.append("temperature")

                cleaned_motivation = _clean_text(input_data.motivation)
                if cleaned_motivation is not None and (
                    not only_fill_missing or _is_missing_text(stakeholder.motivation)
                ):
                    stakeholder.motivation = cleaned_motivation
                    updated_fields.append("motivation")

                if input_data.pain_points is not None and (
                    not only_fill_missing or not stakeholder.pain_points
                ):
                    stakeholder.pain_points = input_data.pain_points
                    updated_fields.append("pain_points")

                if updated_fields:
                    stakeholder.updated_at = datetime.now(UTC)
                    await session.commit()
                    await session.refresh(stakeholder)

                    # Dispatch research if stakeholder was reactivated
                    if "approval_status" not in updated_fields and (
                        stakeholder.stakeholder_research is None
                        and stakeholder.last_researched_at is None
                    ):
                        try:
                            from src.workers.deal_worker.utils.stakeholder_research_dispatch import (
                                dispatch_stakeholder_research_async,
                            )

                            await dispatch_stakeholder_research_async(
                                organisation_id=organisation_id,
                                stakeholder_id=stakeholder.participant_id,
                                deal_id=actual_deal_id,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to dispatch research for reactivated %s",
                                stakeholder.participant_id,
                                exc_info=True,
                            )

                action = "updated" if updated_fields else "no_change"
                return _with_action(
                    _format_upsert_output(
                        stakeholder, action=action, updated_fields=updated_fields
                    ),
                    action,
                )

            if not input_data.create_if_missing:
                return {
                    "email": email,
                    "action": "not_found",
                    "updated": False,
                    "updated_fields": [],
                    "message": "Stakeholder not found and creation disabled",
                }

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
                )
            except StakeholdersServiceError:
                logger.exception("upsert_stakeholder: service create failed")
                return None

            # Dispatch stakeholder research for newly created stakeholder
            try:
                from src.workers.deal_worker.utils.stakeholder_research_dispatch import (
                    dispatch_stakeholder_research_async,
                )

                await dispatch_stakeholder_research_async(
                    organisation_id=organisation_id,
                    stakeholder_id=new_stakeholder.stakeholder_id,
                    deal_id=actual_deal_id,
                )
            except Exception:
                logger.warning(
                    "Failed to dispatch research for %s",
                    new_stakeholder.stakeholder_id,
                    exc_info=True,
                )

            return _with_action(_format_upsert_output(new_stakeholder, action="created"), "created")
        except Exception:
            await session.rollback()
            logger.exception("upsert_stakeholder: database error")
            return None


def create_upsert_stakeholder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create an upsert stakeholder tool bound to a context."""

    async def upsert_stakeholder_tool(
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
        only_fill_missing: bool = True,
        create_if_missing: bool = True,
    ) -> dict[str, Any] | None:
        """Upsert stakeholder by email in the current deal.

        deal_id: Deal UUID. If not provided, uses the current deal context.

        IMPORTANT: Do NOT call this for internal team members. Only external
        customer/prospect contacts should be added as stakeholders. Internal
        emails are rejected automatically.

        Strategy:
        1. Attempt update by email (fill missing fields by default)
        2. If not found and create_if_missing=True, create the stakeholder
        """
        logger.info("[TOOL CALLED] upsert_stakeholder - email='%s'", email)

        if not context.organisation_id:
            logger.warning("upsert_stakeholder unavailable: missing organisation_id")
            return None

        effective_deal_id = deal_id or (str(context.deal_id) if context.deal_id else None)
        if not effective_deal_id:
            logger.warning("upsert_stakeholder unavailable: no deal_id provided or in context")
            return None

        normalized_stance = _normalize_optional_stance(stance)
        normalized_decision_power = _normalize_optional_decision_power(decision_power)
        normalized_temperature = _normalize_optional_temperature(temperature)

        input_data = UpsertStakeholderInput(
            email=email,
            name=name,
            job_title=job_title,
            phone_number=phone_number,
            department=department,
            stance=normalized_stance,
            decision_power=normalized_decision_power,
            temperature=normalized_temperature,
            motivation=motivation,
            pain_points=pain_points,
            only_fill_missing=only_fill_missing,
            create_if_missing=create_if_missing,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="UpsertStakeholder",
            tool_func=upsert_stakeholder,
            parameters={"context": context, "deal_id": effective_deal_id, "input_data": input_data},
            description="Updating stakeholder",
            fallback_value=None,
        )

        if result:
            action = result.get("action")
            if action == "rejected_internal_user":
                logger.info(
                    "[TOOL RESULT] upsert_stakeholder rejected internal email=%s",
                    result.get("email"),
                )
            else:
                logger.info(
                    "[TOOL RESULT] upsert_stakeholder action=%s email=%s",
                    action,
                    result.get("email"),
                )
        else:
            logger.info("[TOOL RESULT] upsert_stakeholder failed")

        return dict(result) if result else None

    return upsert_stakeholder_tool
