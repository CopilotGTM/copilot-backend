"""Upsert stakeholder tool for deal-level agents.

Single agent-facing stakeholder write tool that updates by email and creates when missing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


class UpsertStakeholderInput(BaseModel):
    email: Annotated[str, Field(description="Stakeholder email (required)")]
    name: Annotated[str | None, Field(default=None, description="Full name")] = None
    job_title: Annotated[str | None, Field(default=None, description="Job title")] = None
    department: Annotated[str | None, Field(default=None, description="Department")] = None
    stance: Annotated[StakeholderStance | None, Field(default=None)] = None
    decision_power: Annotated[DecisionPower | None, Field(default=None)] = None
    temperature: Annotated[StakeholderTemperature | None, Field(default=None)] = None
    motivation: Annotated[str | None, Field(default=None, description="Motivation")] = None
    pain_points: Annotated[list[str] | None, Field(default=None, description="Pain points")] = None
    champion_potential: Annotated[float | None, Field(default=None, ge=0.0, le=100.0)] = None
    engagement_score: Annotated[float | None, Field(default=None, ge=0.0, le=100.0)] = None
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
    stakeholder: DealStakeholder,
    *,
    action: str,
    updated_fields: list[str] | None = None,
) -> dict[str, Any]:
    updates = updated_fields or []
    return {
        "stakeholder_id": str(stakeholder.stakeholder_id),
        "email": stakeholder.email,
        "name": stakeholder.name,
        "job_title": stakeholder.job_title,
        "department": stakeholder.department,
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
        "champion_potential": stakeholder.champion_potential,
        "engagement_score": stakeholder.engagement_score,
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
    input_data: UpsertStakeholderInput,
) -> dict[str, Any] | None:
    """Upsert stakeholder by email in the current deal."""
    organisation_id = context.organisation_id
    deal_id = context.deal_id

    if not organisation_id or not deal_id:
        return None

    email = input_data.email.strip().lower()
    if not email:
        return None

    c = DealStakeholder.__table__.c
    stmt = select(DealStakeholder).where(
        c.organisation_id == organisation_id,
        c.deal_id == deal_id,
        c.email == email,
    )

    async with context.acquire_session() as session:
        try:
            existing_result = await session.execute(stmt)
            stakeholder = existing_result.scalar_one_or_none()

            if stakeholder:
                updated_fields: list[str] = []
                only_fill_missing = input_data.only_fill_missing

                cleaned_name = _clean_text(input_data.name)
                if cleaned_name is not None and (
                    not only_fill_missing or _is_missing_text(stakeholder.name)
                ):
                    stakeholder.name = cleaned_name
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

                if input_data.stance is not None and (
                    not only_fill_missing or _is_unknown_enum(stakeholder.stance)
                ):
                    stakeholder.stance = input_data.stance
                    updated_fields.append("stance")

                if input_data.decision_power is not None and (
                    not only_fill_missing or _is_unknown_enum(stakeholder.decision_power)
                ):
                    stakeholder.decision_power = input_data.decision_power
                    updated_fields.append("decision_power")

                if input_data.temperature is not None and (
                    not only_fill_missing or _is_unknown_enum(stakeholder.temperature)
                ):
                    stakeholder.temperature = input_data.temperature
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

                if input_data.champion_potential is not None and (
                    not only_fill_missing or (stakeholder.champion_potential or 0.0) <= 0.0
                ):
                    stakeholder.champion_potential = input_data.champion_potential
                    updated_fields.append("champion_potential")

                if input_data.engagement_score is not None and (
                    not only_fill_missing or (stakeholder.engagement_score or 0.0) <= 0.0
                ):
                    stakeholder.engagement_score = input_data.engagement_score
                    updated_fields.append("engagement_score")

                if updated_fields:
                    stakeholder.updated_at = datetime.now(UTC)
                    await session.commit()
                    await session.refresh(stakeholder)

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

            stakeholder_data: dict[str, Any] = {
                "stakeholder_id": uuid4(),
                "organisation_id": organisation_id,
                "integration_id": None,
                "deal_id": deal_id,
                "contact_id": None,
                "email": email,
                "name": _clean_text(input_data.name),
                "job_title": _clean_text(input_data.job_title),
                "department": _clean_text(input_data.department),
                "stance": input_data.stance or StakeholderStance.UNKNOWN,
                "decision_power": input_data.decision_power or DecisionPower.UNKNOWN,
                "temperature": input_data.temperature or StakeholderTemperature.UNKNOWN,
                "motivation": _clean_text(input_data.motivation),
                "pain_points": input_data.pain_points or None,
                "keywords": None,
                "champion_potential": input_data.champion_potential or 0.0,
                "engagement_score": input_data.engagement_score or 0.0,
                "external_links": None,
                "source": StakeholderSource.MANUAL,
                "source_reference_id": None,
                "approval_status": ApprovalStatus.APPROVED,
                "last_contact_at": None,
                "stakeholder_data": None,
                "stakeholder_engagement_history": None,
                "is_deleted": False,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }

            new_stakeholder = DealStakeholder.model_validate(stakeholder_data)
            session.add(new_stakeholder)
            await session.commit()
            await session.refresh(new_stakeholder)
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
        name: str | None = None,
        job_title: str | None = None,
        department: str | None = None,
        stance: StakeholderStance | str | None = None,
        decision_power: DecisionPower | str | None = None,
        temperature: StakeholderTemperature | str | None = None,
        motivation: str | None = None,
        pain_points: list[str] | None = None,
        champion_potential: float | None = None,
        engagement_score: float | None = None,
        only_fill_missing: bool = True,
        create_if_missing: bool = True,
    ) -> dict[str, Any] | None:
        """Upsert stakeholder by email in the current deal.

        Strategy:
        1. Attempt update by email (fill missing fields by default)
        2. If not found and create_if_missing=True, create the stakeholder
        """
        logger.info("[TOOL CALLED] upsert_stakeholder - email='%s'", email)

        if not context.organisation_id:
            logger.warning("upsert_stakeholder unavailable: missing organisation_id")
            return None

        if not context.deal_id:
            logger.warning("upsert_stakeholder unavailable: missing deal_id")
            return None

        normalized_stance = _normalize_optional_stance(stance)
        normalized_decision_power = _normalize_optional_decision_power(decision_power)
        normalized_temperature = _normalize_optional_temperature(temperature)

        input_data = UpsertStakeholderInput(
            email=email,
            name=name,
            job_title=job_title,
            department=department,
            stance=normalized_stance,
            decision_power=normalized_decision_power,
            temperature=normalized_temperature,
            motivation=motivation,
            pain_points=pain_points,
            champion_potential=champion_potential,
            engagement_score=engagement_score,
            only_fill_missing=only_fill_missing,
            create_if_missing=create_if_missing,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="UpsertStakeholder",
            tool_func=upsert_stakeholder,
            parameters={"context": context, "input_data": input_data},
            description=f"Upserting stakeholder: {email}",
            fallback_value=None,
        )

        if result:
            logger.info(
                "[TOOL RESULT] upsert_stakeholder action=%s email=%s",
                result.get("action"),
                result.get("email"),
            )
        else:
            logger.info("[TOOL RESULT] upsert_stakeholder failed")

        return dict(result) if result else None

    return upsert_stakeholder_tool
