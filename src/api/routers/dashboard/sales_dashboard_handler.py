"""Sales Dashboard API routes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.sales_dashboard_models import (
    SalesDashboardDateRange,
    SalesDashboardPreferences,
    SalesDashboardPrimarySegment,
    SalesDashboardQueryParams,
    SalesDashboardResponse,
    UpdateSalesDashboardPreferencesRequest,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.dashboard.dashboard_snapshot_service import DashboardSnapshotService
from src.service.dashboard.sales_dashboard_service import SalesDashboardService

sales_dashboard_router = APIRouter()
logger = get_logger("sales_dashboard_handler")


def get_sales_dashboard_query_params(
    date_range: SalesDashboardDateRange | None = Query(None),
    primary_segment: SalesDashboardPrimarySegment = Query(SalesDashboardPrimarySegment.STAGE),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    min_deal_value: float | None = Query(None, ge=0),
    max_deal_value: float | None = Query(None, ge=0),
    pipeline_ids: list[UUID] | None = Query(None),
    owner_ids: list[UUID] | None = Query(None),
    include_widgets: list[str] | None = Query(None),
) -> SalesDashboardQueryParams:
    return SalesDashboardQueryParams(
        date_range=date_range,
        primary_segment=primary_segment,
        from_date=from_date,
        to_date=to_date,
        min_deal_value=min_deal_value,
        max_deal_value=max_deal_value,
        pipeline_ids=pipeline_ids or [],
        owner_ids=owner_ids or [],
        include_widgets=include_widgets or [],
    )


@sales_dashboard_router.get(
    "/sales",
    response_model=GenericResponse[SalesDashboardResponse],
    summary="Get the application-provided Sales Dashboard",
)
async def get_sales_dashboard(
    query: SalesDashboardQueryParams = Depends(get_sales_dashboard_query_params),
    user: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[SalesDashboardResponse]:
    access_actor = CRMAccessActor(user.user_id, user.user_role, user.user_email)
    snapshot_service = DashboardSnapshotService(
        org_session=org_session,
        master_session=master_session,
        organisation_id=user.organisation_id,
    )
    try:
        if access_actor.is_admin:
            payload = await snapshot_service.get_sales_dashboard(query, user_id=user.user_id)
        else:
            payload = await SalesDashboardService(
                org_session=org_session,
                master_session=master_session,
                organisation_id=user.organisation_id,
                user_id=user.user_id,
                access_actor=access_actor,
            ).get_sales_dashboard(query)
    except Exception:
        logger.exception(
            "Snapshot-backed Sales Dashboard read failed; falling back to live render",
            extra={"organisation_id": str(user.organisation_id), "dashboard_key": "sales"},
        )
        service = SalesDashboardService(
            org_session=org_session,
            master_session=master_session,
            organisation_id=user.organisation_id,
            user_id=user.user_id,
            access_actor=access_actor,
        )
        payload = await service.get_sales_dashboard(query)
    return Response.success_response(
        message="Sales Dashboard retrieved successfully",
        data=payload,
    )


@sales_dashboard_router.get(
    "/sales/preferences",
    response_model=GenericResponse[SalesDashboardPreferences],
    summary="Get current user's Sales Dashboard preferences",
)
async def get_sales_dashboard_preferences(
    user: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[SalesDashboardPreferences]:
    service = SalesDashboardService(
        org_session=None,
        master_session=master_session,
        organisation_id=user.organisation_id,
        user_id=user.user_id,
    )
    payload = await service.get_preferences()
    return Response.success_response(
        message="Sales Dashboard preferences retrieved successfully",
        data=payload,
    )


@sales_dashboard_router.patch(
    "/sales/preferences",
    response_model=GenericResponse[SalesDashboardPreferences],
    summary="Update current user's Sales Dashboard preferences",
)
async def update_sales_dashboard_preferences(
    request: UpdateSalesDashboardPreferencesRequest,
    user: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[SalesDashboardPreferences]:
    service = SalesDashboardService(
        org_session=None,
        master_session=master_session,
        organisation_id=user.organisation_id,
        user_id=user.user_id,
    )
    payload = await service.update_preferences(request)
    return Response.success_response(
        message="Sales Dashboard preferences updated successfully",
        data=payload,
    )
