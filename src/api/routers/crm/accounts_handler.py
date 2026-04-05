"""CRM Accounts API handler - tenant-scoped endpoints for accessing account (company) data."""

from __future__ import annotations

import math
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
from src.api.schemas.crm_account_models import (
    AccountContactItem,
    AccountDealItem,
    AccountDetail,
    AccountListItem,
    AccountsPage,
    CreateAccountRequest,
    EnrichAccountResponse,
    UpdateAccountRequest,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.access.accounts_access_service import (
    AccountsAccessService,
    AccountsAccessServiceError,
)

logger = get_logger("accounts_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

accounts_router = APIRouter()


async def get_accounts_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields an AccountsAccessService instance for the current org."""
    service = AccountsAccessService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("AccountsAccessService close failed", exc_info=True)


@accounts_router.get(
    "/accounts",
    response_model=GenericResponse[AccountsPage],
    tags=["crm", "accounts"],
    summary="List CRM accounts with pagination and filtering",
)
async def list_accounts(
    response: FastAPIResponse,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(25, ge=1, le=100, description="Maximum accounts per page"),
    q: str | None = Query(None, description="Case-insensitive search on name or domain"),
    industry: str | None = Query(None, description="Filter by industry (partial match)"),
    service: AccountsAccessService = Depends(get_accounts_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AccountsPage]:
    """List CRM accounts with optional filters and pagination."""
    try:
        items_raw, total_count = await service.list_accounts(
            page=page, limit=limit, q=q, industry=industry
        )

        items = [AccountListItem(**row) for row in items_raw]

        total_pages = math.ceil(total_count / limit) if limit > 0 else 0
        page_data = AccountsPage(
            items=items,
            page=page,
            limit=limit,
            total_count=total_count,
            total_pages=total_pages,
        )
        return Response.success_response("Accounts retrieved successfully", data=page_data)
    except AccountsAccessServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error listing accounts")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@accounts_router.post(
    "/accounts",
    response_model=GenericResponse[AccountListItem],
    tags=["crm", "accounts"],
    summary="Create a new CRM account",
)
async def create_account(
    response: FastAPIResponse,
    body: CreateAccountRequest,
    service: AccountsAccessService = Depends(get_accounts_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AccountListItem]:
    """Create a new internal CRM account."""
    try:
        company = await service.create_account(
            name=body.name,
            domain=body.domain,
            website=body.website,
            industry=body.industry,
            linkedin_url=body.linkedin_url,
        )

        item = AccountListItem(
            **company.model_dump(),
            contact_count=0,
            deal_count=0,
            pipeline_value=0.0,
        )
        return Response.created_response("Account created successfully", data=item)
    except AccountsAccessServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error creating account")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@accounts_router.get(
    "/accounts/{company_id}",
    response_model=GenericResponse[AccountDetail],
    tags=["crm", "accounts"],
    summary="Get full account detail with contacts and deals",
)
async def get_account_detail(
    company_id: UUID,
    response: FastAPIResponse,
    service: AccountsAccessService = Depends(get_accounts_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AccountDetail]:
    """Get full account detail including contacts, deals, and summary metrics."""
    try:
        data = await service.get_account_detail(company_id)

        contact_items = [
            AccountContactItem(
                contact_id=ct["contact_id"],
                full_name=ct.get("full_name"),
                email=ct.get("email"),
                job_title=ct.get("job_title"),
                deal_count=ct.get("deal_count", 0),
            )
            for ct in data["contacts"]
        ]

        deal_items: list[AccountDealItem] = []
        for dl in data["deals"]:
            # Resolve stage_name and owner_name from deal dict — may come from model_dump
            deal_items.append(
                AccountDealItem(
                    deal_id=dl["deal_id"],
                    name=dl.get("name"),
                    amount=dl.get("amount"),
                    currency=dl.get("currency"),
                    deal_status=dl.get("deal_status"),
                    stage_name=dl.get("stage_name"),
                    owner_name=dl.get("owner_name"),
                )
            )

        detail = AccountDetail(
            **{k: v for k, v in data.items() if k not in ("contacts", "deals")},
            contacts=contact_items,
            deals=deal_items,
        )
        return Response.success_response("Account detail retrieved successfully", data=detail)
    except AccountsAccessServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error retrieving account detail")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@accounts_router.patch(
    "/accounts/{company_id}",
    response_model=GenericResponse[AccountListItem],
    tags=["crm", "accounts"],
    summary="Update an existing CRM account",
)
async def update_account(
    company_id: UUID,
    response: FastAPIResponse,
    body: UpdateAccountRequest,
    service: AccountsAccessService = Depends(get_accounts_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AccountListItem]:
    """Update allowed fields on an existing CRM account."""
    try:
        updates = body.model_dump(exclude_unset=True)
        company = await service.update_account(company_id, updates)

        item = AccountListItem(
            **company.model_dump(),
            contact_count=0,
            deal_count=0,
            pipeline_value=0.0,
        )
        return Response.success_response("Account updated successfully", data=item)
    except AccountsAccessServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error updating account")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@accounts_router.get(
    "/accounts/{company_id}/contacts",
    response_model=GenericResponse[list[AccountContactItem]],
    tags=["crm", "accounts"],
    summary="Get contacts for an account",
)
async def get_account_contacts(
    company_id: UUID,
    response: FastAPIResponse,
    service: AccountsAccessService = Depends(get_accounts_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[AccountContactItem]]:
    """Return contacts whose email domain matches the account's domain."""
    try:
        contacts = await service.get_account_contacts(company_id)

        items = [
            AccountContactItem(
                contact_id=ct.contact_id,
                full_name=ct.full_name,
                email=ct.email,
                job_title=ct.job_title,
                deal_count=0,
            )
            for ct in contacts
        ]
        return Response.success_response("Account contacts retrieved successfully", data=items)
    except AccountsAccessServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error retrieving account contacts")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@accounts_router.get(
    "/accounts/{company_id}/deals",
    response_model=GenericResponse[list[AccountDealItem]],
    tags=["crm", "accounts"],
    summary="Get deals for an account",
)
async def get_account_deals(
    company_id: UUID,
    response: FastAPIResponse,
    service: AccountsAccessService = Depends(get_accounts_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[AccountDealItem]]:
    """Return deals linked to the account via company_external_id."""
    try:
        deals = await service.get_account_deals(company_id)

        items = [
            AccountDealItem(
                deal_id=dl.deal_id,
                name=dl.name,
                amount=dl.amount,
                currency=dl.currency,
                deal_status=dl.deal_status,
                stage_name=dl.stage_name,
                owner_name=dl.owner_name,
            )
            for dl in deals
        ]
        return Response.success_response("Account deals retrieved successfully", data=items)
    except AccountsAccessServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error retrieving account deals")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@accounts_router.post(
    "/accounts/{company_id}/enrich",
    response_model=GenericResponse[EnrichAccountResponse],
    tags=["crm", "accounts"],
    summary="Enrich account with external data",
)
async def enrich_account(
    company_id: UUID,
    response: FastAPIResponse,
    service: AccountsAccessService = Depends(get_accounts_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[EnrichAccountResponse]:
    """Trigger account enrichment.

    Endpoint exists to reserve the API contract for frontend integration.
    Company research pipeline implementation is deferred to a future phase.
    """
    try:
        # Verify org ownership before accepting — prevents probing for account IDs
        await service.get_account_detail(company_id)
        return Response.accepted_response(
            "Enrichment request accepted",
            data=EnrichAccountResponse(
                job_id=None,
                message="Account enrichment will be available in a future release",
            ),
        )
    except AccountsAccessServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error triggering account enrichment")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)
