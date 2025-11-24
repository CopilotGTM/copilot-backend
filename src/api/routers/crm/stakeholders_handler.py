"""CRM Stakeholders API handler."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_models import StakeholderDetail, StakeholderLink
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DealStakeholder,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
)
from src.infra.utils.logger import get_logger

logger = get_logger("stakeholders_handler")

stakeholders_router = APIRouter()


class CreateStakeholderRequest(BaseModel):
    email: str
    name: str | None = None
    job_title: str | None = None
    external_links: list[StakeholderLink] | None = None
    stance: StakeholderStance = StakeholderStance.UNKNOWN
    decision_power: DecisionPower = DecisionPower.UNKNOWN


class UpdateStakeholderRequest(BaseModel):
    stance: StakeholderStance | None = None
    decision_power: DecisionPower | None = None
    motivation: str | None = None
    pain_points: list[str] | None = None
    external_links: list[StakeholderLink] | None = None


@stakeholders_router.get(
    "/{deal_id}/stakeholders",
    response_model=GenericResponse[list[StakeholderDetail]],
    summary="Get stakeholders for a deal",
)
async def get_deal_stakeholders(
    deal_id: UUID,
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """
    Get all stakeholders associated with a specific deal.
    """
    stmt = select(DealStakeholder).where(
        col(DealStakeholder.deal_id) == deal_id,
        col(DealStakeholder.organisation_id) == user_ids.organisation_id,
    )
    result = await session.execute(stmt)
    stakeholders = result.scalars().all()

    return GenericResponse(
        status="SUCCESS",
        message="Stakeholders retrieved successfully",
        data=stakeholders,
    )


@stakeholders_router.post(
    "/{deal_id}/stakeholders",
    response_model=GenericResponse[StakeholderDetail],
    summary="Manually add a stakeholder",
)
async def create_stakeholder(
    deal_id: UUID,
    request: CreateStakeholderRequest,
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """
    Manually add a stakeholder to a deal.
    """
    # Check if already exists
    stmt = select(DealStakeholder).where(
        col(DealStakeholder.deal_id) == deal_id,
        col(DealStakeholder.email) == request.email,
        col(DealStakeholder.organisation_id) == user_ids.organisation_id,
    )
    result = await session.execute(stmt)
    existing = result.scalars().first()

    if existing:
        raise HTTPException(status_code=409, detail="Stakeholder already exists for this deal.")

    new_stakeholder = DealStakeholder(
        organisation_id=user_ids.organisation_id,
        deal_id=deal_id,
        email=request.email,
        name=request.name,
        job_title=request.job_title,
        external_links=request.external_links,
        stance=request.stance,
        decision_power=request.decision_power,
        source=StakeholderSource.MANUAL,
    )
    session.add(new_stakeholder)
    await session.commit()
    await session.refresh(new_stakeholder)

    return GenericResponse(
        status="SUCCESS",
        message="Stakeholder added successfully",
        data=new_stakeholder,
    )


@stakeholders_router.patch(
    "/{deal_id}/stakeholders/{stakeholder_id}",
    response_model=GenericResponse[StakeholderDetail],
    summary="Update stakeholder details",
)
async def update_stakeholder(
    deal_id: UUID,
    stakeholder_id: UUID,
    request: UpdateStakeholderRequest,
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """
    Update manual fields for a stakeholder (Stance, Power, Motivation).
    """
    stmt = select(DealStakeholder).where(
        col(DealStakeholder.stakeholder_id) == stakeholder_id,
        col(DealStakeholder.deal_id) == deal_id,
        col(DealStakeholder.organisation_id) == user_ids.organisation_id,
    )
    result = await session.execute(stmt)
    stakeholder = result.scalars().first()

    if not stakeholder:
        raise HTTPException(status_code=404, detail="Stakeholder not found.")

    if request.stance is not None:
        stakeholder.stance = request.stance
    if request.decision_power is not None:
        stakeholder.decision_power = request.decision_power
    if request.motivation is not None:
        stakeholder.motivation = request.motivation
    if request.pain_points is not None:
        stakeholder.pain_points = request.pain_points
    if request.external_links is not None:
        # Merge or replace? Let's replace for simplicity, or merge if desired.
        # For now, simple replacement of the dict.
        stakeholder.external_links = request.external_links

    await session.commit()
    await session.refresh(stakeholder)

    return GenericResponse(
        status="SUCCESS",
        message="Stakeholder updated successfully",
        data=stakeholder,
    )
