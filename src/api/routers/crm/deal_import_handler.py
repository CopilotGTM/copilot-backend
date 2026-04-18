"""Disabled deal CSV import routes.

The route paths stay in place so the frontend can keep calling the same API
surface, but the old import flow is intentionally unavailable while it is
rebuilt from scratch.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from fastapi import Response as FastAPIResponse
from src.api.dependencies.organisation_dependencies import get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response

deal_import_router = APIRouter()

_DISABLED_IMPORT_MESSAGE = (
    "Deal CSV import is coming soon. The previous import flow has been removed."
)


def _coming_soon_response(response: FastAPIResponse) -> GenericResponse[None]:
    return Response.conflict_error_response(
        message=_DISABLED_IMPORT_MESSAGE,
        response=response,
    )


@deal_import_router.get(
    "/import/template",
    response_model=GenericResponse[None],
    tags=["deals"],
    summary="Deal import template is temporarily unavailable",
)
async def get_deal_import_template(
    response: FastAPIResponse,
    _file_type: str | None = None,
    _user=Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_read),
) -> GenericResponse[None]:
    return _coming_soon_response(response)


@deal_import_router.post(
    "/import/upload-url",
    response_model=GenericResponse[None],
    tags=["deals"],
    summary="Deal CSV import upload is temporarily unavailable",
)
async def create_deal_import_upload_url(
    response: FastAPIResponse,
    _request: dict[str, Any] | None = Body(default=None),
    _user=Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[None]:
    return _coming_soon_response(response)


@deal_import_router.post(
    "/import/process",
    response_model=GenericResponse[None],
    tags=["deals"],
    summary="Deal CSV import processing is temporarily unavailable",
)
async def process_deal_import(
    response: FastAPIResponse,
    _request: dict[str, Any] | None = Body(default=None),
    _user=Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[None]:
    return _coming_soon_response(response)


@deal_import_router.get(
    "/import/jobs/{job_id}/events",
    response_model=GenericResponse[None],
    tags=["deals"],
    summary="Deal import event stream is temporarily unavailable",
)
async def deal_import_stream(
    job_id: UUID,
    response: FastAPIResponse,
    _user=Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_read),
) -> GenericResponse[None]:
    del job_id
    return _coming_soon_response(response)
