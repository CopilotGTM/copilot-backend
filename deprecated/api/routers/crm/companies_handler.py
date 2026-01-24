"""CRM companies API handler."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_company_models import (
    CompanyCreateRequest,
    CompanyListItem,
    CompanyUpdateRequest,
    CompanyWriteResponse,
    CRMCompaniesPage,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.internal.crm_company_service import CRMCompanyService, CRMCompanyServiceError

logger = get_logger(__name__)

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

companies_router = APIRouter()


async def get_company_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    service = CRMCompanyService(session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("CRMCompanyService close failed", exc_info=True)


def _map_company_write_response(company) -> CompanyWriteResponse:
    return CompanyWriteResponse(
        company_id=company.company_id,
        external_id=company.external_id,
        name=company.name,
        domain=company.domain,
        industry=company.industry,
        owner_user_id=company.owner_id,
        created_at=company.created_at,
        updated_at=company.updated_at,
    )


@companies_router.get(
    "/companies",
    response_model=GenericResponse[CRMCompaniesPage],
    tags=["crm", "companies"],
    summary="List internal CRM companies",
)
async def list_companies(
    response: FastAPIResponse,
    limit: int = Query(50, ge=1, le=100, description="Maximum companies per page"),
    cursor: str | None = Query(None, description="Pagination cursor"),
    q: str | None = Query(None, description="Search by name, domain, or industry"),
    service: CRMCompanyService = Depends(get_company_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMCompaniesPage]:
    """List internal CRM companies for the organisation."""
    try:
        companies, next_cursor = await service.list_companies(limit=limit, cursor=cursor, q=q)
        items = [
            CompanyListItem(
                company_id=company.company_id,
                external_id=company.external_id,
                name=company.name,
                domain=company.domain,
                industry=company.industry,
                owner_user_id=company.owner_id,
            )
            for company in companies
        ]
        payload = CRMCompaniesPage(items=items, cursor=next_cursor)
        return Response.success_response(message="Companies retrieved successfully", data=payload)
    except CRMCompanyServiceError as exc:
        logger.warning("Company list error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing companies: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@companies_router.post(
    "/companies",
    response_model=GenericResponse[CompanyWriteResponse],
    status_code=201,
    tags=["crm", "companies"],
    summary="Create an internal CRM company",
)
async def create_company(
    request: CompanyCreateRequest,
    response: FastAPIResponse,
    service: CRMCompanyService = Depends(get_company_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[CompanyWriteResponse]:
    """Create a new internal CRM company."""
    try:
        company = await service.create_company(request.model_dump())
        await service.session.commit()
        await service.session.refresh(company)
        payload = _map_company_write_response(company)
        return Response.created_response(
            message="Company created successfully", data=payload, response=response
        )
    except CRMCompanyServiceError as exc:
        await service.session.rollback()
        logger.warning("Company create error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error creating company: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@companies_router.patch(
    "/companies/{company_id}",
    response_model=GenericResponse[CompanyWriteResponse],
    tags=["crm", "companies"],
    summary="Update an internal CRM company",
)
async def update_company(
    company_id: UUID,
    request: CompanyUpdateRequest,
    response: FastAPIResponse,
    service: CRMCompanyService = Depends(get_company_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[CompanyWriteResponse]:
    """Update fields for an internal CRM company."""
    try:
        company = await service.update_company(company_id, request.model_dump(exclude_unset=True))
        await service.session.commit()
        await service.session.refresh(company)
        payload = _map_company_write_response(company)
        return Response.success_response(message="Company updated successfully", data=payload)
    except CRMCompanyServiceError as exc:
        await service.session.rollback()
        logger.warning("Company update error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error updating company: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@companies_router.delete(
    "/companies/{company_id}",
    response_model=GenericResponse[CompanyWriteResponse],
    tags=["crm", "companies"],
    summary="Delete an internal CRM company",
)
async def delete_company(
    company_id: UUID,
    response: FastAPIResponse,
    service: CRMCompanyService = Depends(get_company_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[CompanyWriteResponse]:
    """Soft delete an internal CRM company."""
    try:
        company = await service.delete_company(company_id)
        await service.session.commit()
        await service.session.refresh(company)
        payload = _map_company_write_response(company)
        return Response.success_response(message="Company deleted successfully", data=payload)
    except CRMCompanyServiceError as exc:
        await service.session.rollback()
        logger.warning("Company delete error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error deleting company: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
