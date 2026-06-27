"""CRM Accounts API handler - tenant-scoped endpoints for accessing account (company) data."""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.crm_write_guard import require_crm_write_scope
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.presenters.crm.activity_feed_presenter import present_activity_feed_page
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_account_models import (
    AccountContactItem,
    AccountDealItem,
    AccountDetail,
    AccountDetailContactItem,
    AccountListItem,
    AccountSearchItem,
    AccountSearchPage,
    AccountsPage,
    AccountSuggestion,
    CreateAccountRequest,
    UpdateAccountRequest,
)
from src.api.schemas.crm_activity_feed_models import (
    ActivityFeedPage,
    ActivityFeedQueryParams,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.crm import CRMActivityObjectType
from src.infra.utils.logger import get_logger
from src.service.crm.accounts.contracts import (
    AccountCreateCommand,
    AccountReadServiceError,
    AccountWriteContext,
    AccountWriteServiceError,
)
from src.service.crm.accounts.read_service import AccountReadService
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.activity_feed_read_service import (
    ActivityFeedReadService,
    ActivityFeedReadServiceError,
)
from src.workers.deal_worker.utils.account_research_dispatch import (
    dispatch_account_research_async,
)

logger = get_logger("accounts_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

accounts_router = APIRouter()


async def get_account_read_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Yield a read-only account service bound to the authenticated organisation."""
    service = AccountReadService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("AccountReadService close failed", exc_info=True)


async def get_account_write_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Yield an account writer; endpoint handlers still own commit/rollback."""
    service = AccountWriteService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.session.close()
        except Exception:
            logger.debug("AccountWriteService close failed", exc_info=True)


async def get_account_activity_feed_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Yield an account activity-feed service bound to the authenticated organisation."""
    service = ActivityFeedReadService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Account activity-feed service close failed", exc_info=True)


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
    service: AccountReadService = Depends(get_account_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AccountsPage]:
    """List CRM accounts with UI summary metrics for the authenticated org."""
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
    except AccountReadServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error listing accounts")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@accounts_router.get(
    "/accounts/suggest",
    response_model=GenericResponse[AccountSuggestion | None],
    tags=["crm", "accounts"],
    summary="Suggest an account by email domain",
)
async def suggest_account(
    response: FastAPIResponse,
    email_domain: str = Query(..., description="Email domain to match (e.g., 'acme.com')"),
    service: AccountReadService = Depends(get_account_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AccountSuggestion | None]:
    """Suggest an account by domain for contact/account picker workflows."""
    try:
        suggestion = await service.suggest_account_by_domain(email_domain)
        if suggestion:
            data = AccountSuggestion(**suggestion)
            return Response.success_response("Account suggestion found", data=data)
        return Response.success_response("No matching account found", data=None)
    except Exception:
        logger.exception("Unexpected error suggesting account")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@accounts_router.get(
    "/accounts/search",
    response_model=GenericResponse[AccountSearchPage],
    tags=["crm", "accounts"],
    summary="Search CRM accounts with cursor pagination",
)
async def search_accounts(
    response: FastAPIResponse,
    q: str | None = Query(None, description="Case-insensitive search on name, domain, or website"),
    limit: int = Query(20, ge=1, le=50, description="Maximum accounts per cursor page"),
    cursor: str | None = Query(None, description="Opaque cursor returned by the previous page"),
    service: AccountReadService = Depends(get_account_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AccountSearchPage]:
    """Return lightweight account rows for pickers and typeahead dropdowns."""
    try:
        items_raw, next_cursor = await service.search_accounts(q=q, limit=limit, cursor=cursor)
        items = [AccountSearchItem(**row) for row in items_raw]
        page_data = AccountSearchPage(
            items=items,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )
        return Response.success_response("Accounts search results retrieved", data=page_data)
    except AccountReadServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error searching accounts")
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
    user_ids: UserIDs = Depends(get_current_user),
    service: AccountWriteService = Depends(get_account_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("accounts:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AccountListItem]:
    """Create a new internal CRM account and commit the API-owned transaction."""
    try:
        result = await service.create_account(
            AccountCreateCommand(
                name=body.name,
                domain=body.domain,
                website=body.website,
                industry=body.industry,
                linkedin_url=body.linkedin_url,
            ),
            context=AccountWriteContext(
                source="api",
                actor_user_id=user_ids.user_id,
                actor_name=getattr(user_ids, "user_name", None),
                actor_email=getattr(user_ids, "user_email", None),
                created_via="accounts_app",
                duplicate_scope="strict_org",
            ),
        )
        # AccountWriteService never commits because deal workflows may combine
        # account writes with participant/deal writes in one transaction.
        await service.session.commit()

        company = result.account
        company_id = getattr(company, "company_id", None)
        if company_id:
            try:
                await dispatch_account_research_async(
                    organisation_id=user_ids.organisation_id,
                    company_id=company_id,
                    user_id=user_ids.user_id,
                    trigger="account_created",
                )
            except Exception:
                logger.warning(
                    "Account research dispatch failed after account create",
                    extra={
                        "event": "account_research_dispatch_failed",
                        "organisation_id": str(user_ids.organisation_id),
                        "company_id": str(company_id),
                    },
                    exc_info=True,
                )

        item = AccountListItem(
            **company.model_dump(),
            contact_count=0,
            deal_count=0,
            pipeline_value=0.0,
        )
        return Response.created_response("Account created successfully", data=item)
    except AccountWriteServiceError as exc:
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
    service: AccountReadService = Depends(get_account_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AccountDetail]:
    """Get account detail after AccountReadService verifies tenant ownership."""
    try:
        data = await service.get_account_detail(company_id)

        contact_items = [
            AccountDetailContactItem(
                contact_id=ct["contact_id"],
                full_name=ct.get("full_name"),
                email=ct.get("email"),
                job_title=ct.get("job_title"),
            )
            for ct in data["contacts"]
        ]

        deal_items: list[AccountDealItem] = []
        for dl in data["deals"]:
            # `data["deals"]` comes from model_dump, so optional fields are read
            # defensively to tolerate older rows that lack stage/owner snapshots.
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
    except AccountReadServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error retrieving account detail")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@accounts_router.get(
    "/accounts/{company_id}/activity-feed",
    response_model=GenericResponse[ActivityFeedPage],
    tags=["crm", "accounts"],
    summary="Get account activity feed",
)
async def get_account_activity_feed(
    company_id: UUID,
    response: FastAPIResponse,
    service: ActivityFeedReadService = Depends(get_account_activity_feed_service),
    query: ActivityFeedQueryParams = Depends(),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ActivityFeedPage]:
    """Get activity-feed cards linked to an account/company through crm_activity_links."""
    try:
        records, next_cursor, total_count = await service.list_object_activities(
            object_type=CRMActivityObjectType.COMPANY.value,
            object_id=company_id,
            limit=query.limit,
            cursor=query.cursor,
            activity_types=query.activity_types,
            source_types=query.source_types,
            start_date=query.start_date,
            end_date=query.end_date,
            sort_order=query.sort,
        )
        return Response.success_response(
            message="Activity feed retrieved successfully",
            data=present_activity_feed_page(
                object_type=CRMActivityObjectType.COMPANY.value,
                object_id=company_id,
                records=records,
                total_count=total_count,
                cursor=next_cursor,
            ),
        )
    except ActivityFeedReadServiceError as exc:
        logger.warning("Account activity feed access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected error getting activity feed for account %s", company_id)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG,
            status_code=500,
            response=response,
        )


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
    user_ids: UserIDs = Depends(get_current_user),
    service: AccountWriteService = Depends(get_account_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("accounts:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AccountListItem]:
    """Update an internal CRM account and commit the API-owned transaction."""
    try:
        updates = body.model_dump(exclude_unset=True)
        company = await service.update_account(
            company_id,
            updates,
            context=AccountWriteContext(
                source="api",
                actor_user_id=user_ids.user_id,
                created_via="accounts_app",
                duplicate_scope="strict_org",
            ),
        )
        # Keep create/update transaction ownership symmetric. Without this
        # commit, the API can return success while the account update rolls back.
        await service.session.commit()

        item = AccountListItem(
            **company.model_dump(),
            contact_count=0,
            deal_count=0,
            pipeline_value=0.0,
        )
        return Response.success_response("Account updated successfully", data=item)
    except AccountWriteServiceError as exc:
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
    limit: int = Query(25, ge=1, le=100, description="Maximum contacts to return"),
    service: AccountReadService = Depends(get_account_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[AccountContactItem]]:
    """Return account contacts after the read service verifies account ownership."""
    try:
        contacts = await service.list_account_contacts(company_id, limit=limit)

        items = [
            AccountContactItem(
                contact_id=ct["contact_id"],
                full_name=ct.get("full_name"),
                email=ct.get("email"),
                job_title=ct.get("job_title"),
                deal_count=ct.get("deal_count", 0),
            )
            for ct in contacts
        ]
        return Response.success_response("Account contacts retrieved successfully", data=items)
    except AccountReadServiceError as exc:
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
    service: AccountReadService = Depends(get_account_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[AccountDealItem]]:
    """Return active deals linked to one tenant-owned account."""
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
    except AccountReadServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error retrieving account deals")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)
