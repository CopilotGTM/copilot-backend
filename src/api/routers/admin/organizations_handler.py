"""Super-admin organisation lifecycle routes and creation-job streaming.

Creation routes enqueue the worker that writes master organisation state and
shared-tenant defaults. Shared database infrastructure is deployment-owned.
"""

from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import (
    get_master_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_admin_read
from src.api.schemas.admin_job_models import (
    OrganisationJobCreateResponse,
)
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.metadata.countries_metadata import get_allowed_org_countries
from src.domain.models.admin_org_models import (
    CountriesResponse,
    CountryOption,
    CreateOrganizationRequest,
    CreateOrganizationResponse,
    ListOrganizationResponse,
    PaginationOrganizations,
    UpdateOrganisationAppSettingsRequest,
    UpdateOrganisationStatusRequest,
    UpdateOrganisationStatusResponse,
    UpdateOrganizationRequest,
)
from src.domain.models.master_tables import AdminRole, SuperAdmin
from src.domain.models.master_tables.organisation_job_models import (
    OrganisationJobType,
)
from src.infra.auth.admin_auth import admin_auth
from src.infra.config.constant import AdminOrganizationErrors
from src.infra.utils.logger import get_logger
from src.service.admin.admin_organisation_service import (
    AdminOrganisationService,
    NoUpdateDataError,
    OrganisationNotFoundError,
)
from src.service.admin.organisation_job_sse_service import OrganisationJobSSEService
from src.workers.organization_worker.client import (
    send_organization_create_task,
    send_organization_task_and_wait_async,
)
from sse_starlette.sse import EventSourceResponse

logger = get_logger("admin_organisations_handler")
router = APIRouter()


async def get_admin_organisation_service(
    session: AsyncSession = Depends(get_master_async_session),
) -> AsyncGenerator[AdminOrganisationService, None]:
    """Dependency that provides AdminOrganisationService with session ownership.

    The service owns the session and closes it in the finally block.
    """
    service = AdminOrganisationService(session)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("AdminOrganisationService close failed", exc_info=True)


@router.get(
    "/countries",
    response_model=GenericResponse[CountriesResponse],
    tags=["admin_organisations"],
)
async def list_allowed_countries(
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    _rl_admin_read: None = Depends(rate_limit_admin_read),
) -> GenericResponse[CountriesResponse]:
    """Return the list of allowed countries for organisation provisioning.

    Backed by a code-owned metadata registry for now; future: DB-backed table.
    """
    if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
        return Response.forbidden_error_response(
            AdminOrganizationErrors.ADMIN_ACCESS_REQUIRED, response=response
        )

    allowed = get_allowed_org_countries()
    countries = [CountryOption(code=c.code, name=c.name) for c in allowed]
    return Response.success_response(
        message="Countries retrieved successfully",
        data=CountriesResponse(countries=countries),
    )


@router.post(
    "/create_organisation",
    response_model=GenericResponse[OrganisationJobCreateResponse],
    status_code=202,
    tags=["admin_organisations"],
)
async def create_organization(
    request: CreateOrganizationRequest,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminOrganisationService = Depends(get_admin_organisation_service),
) -> GenericResponse[OrganisationJobCreateResponse]:
    """Queue master organisation creation and shared-tenant default setup.

    This endpoint is restricted to super admins and returns 202 Accepted with
    a job ID. The deployment owns the shared schema, runtime role, and migrations.
    """
    try:
        current_user = current_admin.admin_id
        logger.info(f"Creating organization for admin: {current_user}")
        logger.info(
            f"Organization creation request for '{request.organisation_name}' "
            f"by admin: {current_admin.email}"
        )

        if current_admin.role not in [AdminRole.SUPER_ADMIN]:
            logger.warning(
                f"Unauthorized attempt to create organization by user: {current_user}. "
                "Super admin access required."
            )
            return Response.forbidden_error_response(
                AdminOrganizationErrors.SUPER_ADMIN_ACCESS_REQUIRED, response=response
            )

        org_details = request.model_dump(exclude_unset=True, exclude={"organisation_name"})
        logger.info(f"Organization details: {org_details}")

        # Persist the job before dispatch so SSE recovery has durable state.
        job_result = await service.create_job(
            job_type=OrganisationJobType.CREATE,
            admin_id=current_user,
            organisation_id=None,
            request_payload={
                "organisation_name": request.organisation_name,
                **org_details,
            },
        )

        # Fire task (no wait)
        await send_organization_create_task(
            job_id=str(job_result.job_id),
            admin_id=str(current_user),
            organisation_name=request.organisation_name,
            org_details=org_details,
        )

        response_data = OrganisationJobCreateResponse(
            job_id=job_result.job_id,
            job_type=job_result.job_type,
            status=job_result.status,
            message="Organisation creation job accepted",
            stream_url=f"/admin/organisations/jobs/{job_result.job_id}/stream",
        )

        logger.info(
            f"Organisation creation job accepted: {job_result.job_id} "
            f"for '{request.organisation_name}'"
        )
        return Response.accepted_response(
            message="Organisation creation job accepted",
            data=response_data,
            response=response,
        )

    except Exception as e:
        logger.error(
            f"Error creating organization '{request.organisation_name}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=f"Failed to create organization - {e}", response=response
        )


@router.post(
    "/create_organisation_sync",
    response_model=GenericResponse[CreateOrganizationResponse],
    tags=["admin_organisations"],
    deprecated=True,
)
async def create_organization_sync(
    request: CreateOrganizationRequest,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: AsyncSession = Depends(get_master_async_session),
) -> GenericResponse[CreateOrganizationResponse]:
    """
    Create a new organization synchronously (DEPRECATED).

    This endpoint waits for the organisation to be fully provisioned before
    returning. Use POST /create_organisation instead with SSE streaming
    for better UX and no request timeouts.
    """
    try:
        current_user = current_admin.admin_id
        logger.info(f"[DEPRECATED] Sync creating organization for admin: {current_user}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN]:
            return Response.forbidden_error_response(
                AdminOrganizationErrors.SUPER_ADMIN_ACCESS_REQUIRED, response=response
            )

        org_details = request.model_dump(exclude_unset=True, exclude={"organisation_name"})

        # Use deprecated wait function
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            task_result = await send_organization_task_and_wait_async(
                "organization.create",
                kwargs={
                    "admin_id": str(current_user),
                    "organisation_name": request.organisation_name,
                    "org_details": org_details,
                },
                queue="organization",
                routing_key="organization",
                timeout=120,
            )

        resp_data = CreateOrganizationResponse(**task_result)
        return Response.success_response(
            message="Organization created successfully", data=resp_data
        )

    except Exception as e:
        logger.error(f"Error creating organization: {e}", exc_info=True)
        return Response.internal_server_error_response(
            message=f"Failed to create organization - {e}", response=response
        )


@router.get(
    "/list_organisations",
    response_model=GenericResponse[PaginationOrganizations],
    tags=["admin_organisations"],
)
async def list_organizations(
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(
        None,
        description="Opaque cursor returned by the previous call (omit for first page)",
    ),
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminOrganisationService = Depends(get_admin_organisation_service),
    _rl_admin_read: None = Depends(rate_limit_admin_read),
) -> GenericResponse[PaginationOrganizations]:
    """
    Retrieve a list of all organizations with pagination.
    This endpoint is restricted to super admins.
    """
    try:
        logger.info(f"Request to list organizations by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(
                f"Unauthorized attempt to list organizations by user: {current_admin.admin_id}."
            )
            return Response.forbidden_error_response(
                AdminOrganizationErrors.SUPER_ADMIN_ACCESS_REQUIRED, response=response
            )

        paginated_orgs = await service.list_organisations(
            limit=limit,
            cursor=cursor,
        )

        logger.info(f"Successfully retrieved {len(paginated_orgs.organizations)} organizations.")
        return Response.success_response(
            message="Organizations retrieved successfully", data=paginated_orgs
        )

    except Exception as e:
        logger.error(
            f"Error listing organizations: {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=f"Failed to list organizations - {e}", response=response
        )


@router.get(
    "/all_organisations_history",
    tags=["admin_organisations"],
)
async def get_all_organisation_history(
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=20),
    cursor: str | None = None,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminOrganisationService = Depends(get_admin_organisation_service),
    _rl_admin_read: None = Depends(rate_limit_admin_read),
):
    """
    Retrieve the action history for all organizations.
    """
    try:
        if current_admin.role not in [AdminRole.SUPER_ADMIN]:
            return Response.forbidden_error_response(
                AdminOrganizationErrors.SUPER_ADMIN_ACCESS_REQUIRED, response=response
            )

        paginated_history = await service.get_all_organisations_history(
            limit=limit,
            cursor=cursor,
        )

        return Response.success_response(
            message="All organization history retrieved successfully",
            data=paginated_history,
        )

    except Exception as e:
        logger.error(f"Error retrieving all organization history: {e}", exc_info=True)
        return Response.internal_server_error_response(
            message="Failed to retrieve all organization history", response=response
        )


@router.get(
    "/{organisation_id}",
    response_model=GenericResponse[ListOrganizationResponse],
    tags=["admin_organisations"],
)
async def get_organization(
    organisation_id: UUID,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminOrganisationService = Depends(get_admin_organisation_service),
    _rl_admin_read: None = Depends(rate_limit_admin_read),
) -> GenericResponse[ListOrganizationResponse]:
    """
    Retrieve a single organization by its ID.
    This endpoint is restricted to super admins and admins.
    """
    try:
        logger.info(
            f"Request to get organization '{organisation_id}' by admin: {current_admin.email}"
        )

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(
                f"Unauthorized attempt to get organization by user: {current_admin.admin_id}."
            )
            return Response.forbidden_error_response(
                AdminOrganizationErrors.ADMIN_ACCESS_REQUIRED, response=response
            )

        org_response = await service.get_organisation(organisation_id=organisation_id)

        logger.info(f"Successfully retrieved organization with ID: {organisation_id}")
        return Response.success_response(
            message="Organization retrieved successfully", data=org_response
        )

    except OrganisationNotFoundError:
        logger.warning(f"Organization with ID '{organisation_id}' not found.")
        return Response.not_found_error_response(
            AdminOrganizationErrors.ORGANIZATION_NOT_FOUND, response=response
        )
    except Exception as e:
        logger.error(
            f"Error retrieving organization '{organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=f"Failed to retrieve organization - {e}", response=response
        )


@router.put(
    "/{organisation_id}/update",
    response_model=GenericResponse[ListOrganizationResponse],
    tags=["admin_organisations"],
)
async def update_organization(
    organisation_id: UUID,
    request: UpdateOrganizationRequest,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminOrganisationService = Depends(get_admin_organisation_service),
) -> GenericResponse[ListOrganizationResponse]:
    """
    Update an organization's details.
    This endpoint is restricted to super admins.
    """
    try:
        logger.info(
            f"Update request for organization '{organisation_id}' by admin: {current_admin.email}"
        )

        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(
                f"Unauthorized attempt to update organization by user: {current_admin.admin_id}."
            )
            return Response.forbidden_error_response(
                AdminOrganizationErrors.SUPER_ADMIN_ACCESS_REQUIRED, response=response
            )

        update_data = request.dict(exclude_unset=True)
        if not update_data:
            return Response.bad_request_error_response(
                AdminOrganizationErrors.NO_UPDATE_DATA, response=response
            )

        org_response = await service.update_organisation(
            organisation_id=organisation_id,
            admin_id=current_admin.admin_id,
            update_data=update_data,
        )

        logger.info(f"Successfully updated organization: {org_response.organisation_name}")
        return Response.success_response(
            message="Organization updated successfully",
            data=org_response,
        )

    except OrganisationNotFoundError:
        logger.warning(f"Organization with ID '{organisation_id}' not found.")
        return Response.not_found_error_response(
            AdminOrganizationErrors.ORGANIZATION_NOT_FOUND, response=response
        )
    except NoUpdateDataError:
        logger.warning(f"No update data provided for organization '{organisation_id}'.")
        return Response.bad_request_error_response(
            AdminOrganizationErrors.NO_UPDATE_DATA, response=response
        )
    except ValueError as ve:
        logger.warning(f"Value error while updating organization: {ve}")
        return Response.bad_request_error_response(message=str(ve), response=response)
    except Exception as e:
        logger.error(
            f"Error updating organization '{organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message="Failed to update organization", response=response
        )


@router.put(
    "/{organisation_id}/status",
    response_model=GenericResponse[UpdateOrganisationStatusResponse],
    tags=["admin_organisations"],
)
async def update_organisation_status(
    organisation_id: UUID,
    request: UpdateOrganisationStatusRequest,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminOrganisationService = Depends(get_admin_organisation_service),
) -> GenericResponse[UpdateOrganisationStatusResponse]:
    """
    Update the status of an organization (e.g., approve, reject, suspend).
    This endpoint is restricted to super admins.
    """
    try:
        logger.info(
            f"Status update request for organization '{organisation_id}' to '{request.status}' by admin: {current_admin.email}"
        )

        if current_admin.role not in [AdminRole.SUPER_ADMIN]:
            logger.warning(
                f"Unauthorized attempt to update organization status by user: {current_admin.admin_id}."
            )
            return Response.forbidden_error_response(
                AdminOrganizationErrors.SUPER_ADMIN_ACCESS_REQUIRED, response=response
            )

        status_result = await service.update_organisation_status(
            organisation_id=organisation_id,
            admin_id=current_admin.admin_id,
            new_status=request.status,
            note=request.note,
        )

        logger.info(f"Successfully updated status for organization to {status_result.status}")
        return Response.success_response(
            message=f"Organization status updated to '{status_result.status.value}'",
            data=status_result,
        )

    except OrganisationNotFoundError:
        logger.warning(f"Organization with ID '{organisation_id}' not found.")
        return Response.not_found_error_response(
            AdminOrganizationErrors.ORGANIZATION_NOT_FOUND, response=response
        )
    except ValueError as ve:
        logger.warning(f"Value error while updating organization status: {ve}")
        return Response.bad_request_error_response(message=str(ve), response=response)
    except Exception as e:
        logger.error(
            f"Error updating organization status for '{organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message="Failed to update organization status", response=response
        )


@router.patch(
    "/{organisation_id}/app-settings",
    response_model=GenericResponse[ListOrganizationResponse],
    tags=["admin_organisations"],
)
async def update_organisation_app_settings(
    organisation_id: UUID,
    request: UpdateOrganisationAppSettingsRequest,
    response: FastAPIResponse,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminOrganisationService = Depends(get_admin_organisation_service),
) -> GenericResponse[ListOrganizationResponse]:
    """Update admin-controlled organisation app settings."""
    try:
        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            return Response.forbidden_error_response(
                AdminOrganizationErrors.ADMIN_ACCESS_REQUIRED, response=response
            )

        org_response = await service.update_organisation_app_settings(
            organisation_id=organisation_id,
            admin_id=current_admin.admin_id,
            settings=request,
        )
        return Response.success_response(
            message="Organization app settings updated successfully",
            data=org_response,
        )
    except OrganisationNotFoundError:
        return Response.not_found_error_response(
            AdminOrganizationErrors.ORGANIZATION_NOT_FOUND, response=response
        )
    except ValueError as ve:
        return Response.bad_request_error_response(message=str(ve), response=response)
    except Exception as e:
        logger.error(
            f"Error updating organization app settings for '{organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message="Failed to update organization app settings", response=response
        )


@router.get(
    "/{organisation_id}/history",
    tags=["admin_organisations"],
)
async def get_organisation_history(
    organisation_id: UUID,
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=20),
    cursor: str | None = None,
    current_admin: SuperAdmin = Depends(admin_auth),
    service: AdminOrganisationService = Depends(get_admin_organisation_service),
    _rl_admin_read: None = Depends(rate_limit_admin_read),
):
    """
    Retrieve the action history for a specific organization.
    """
    try:
        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            return Response.forbidden_error_response(
                AdminOrganizationErrors.ADMIN_ACCESS_REQUIRED, response=response
            )

        paginated_history = await service.get_organisation_history(
            organisation_id=organisation_id,
            limit=limit,
            cursor=cursor,
        )

        return Response.success_response(
            message="Organization history retrieved successfully", data=paginated_history
        )

    except OrganisationNotFoundError:
        logger.warning(f"Organization with ID '{organisation_id}' not found.")
        return Response.not_found_error_response(
            AdminOrganizationErrors.ORGANIZATION_NOT_FOUND, response=response
        )
    except Exception as e:
        logger.error(
            f"Error retrieving history for organization '{organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message="Failed to retrieve organization history", response=response
        )


# =============================================================================
# SSE STREAMING ENDPOINT
# =============================================================================


@router.get(
    "/jobs/{job_id}/stream",
    tags=["admin_organisation_jobs"],
)
async def stream_job_status(
    job_id: UUID,
    current_admin: SuperAdmin = Depends(admin_auth),
):
    """
    Stream organisation job status updates via SSE.

    Subscribes to Redis pub/sub channel for real-time job progress updates.
    Stream ends when job reaches terminal state (completed/failed/cancelled).

    Event types:
        - status: Job status update with progress
        - complete: Job finished (terminal state)
        - ping: Heartbeat (every 25 seconds)
        - error: Error occurred
        - timeout: Stream timeout (10 minutes default)
    """
    logger.info(f"Starting SSE stream for job {job_id}, admin {current_admin.admin_id}")

    if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
        # For SSE, we return an error event instead of a JSON response
        async def error_gen():
            """Emit the authorization failure using the endpoint's SSE contract."""
            yield {
                "event": "error",
                "data": '{"message": "Access denied", "code": "FORBIDDEN"}',
            }

        return EventSourceResponse(
            error_gen(),
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def event_generator():
        """Relay durable job progress until the stream reaches a terminal state."""
        service = OrganisationJobSSEService(current_admin.admin_id)
        try:
            async for evt in service.stream_job_events(
                job_id=job_id,
                timeout_seconds=600,  # 10 minutes
                heartbeat_interval=25.0,
            ):
                yield evt
        finally:
            try:
                await service.close()
            except Exception:
                logger.debug("SSE service close failed", exc_info=True)

    return EventSourceResponse(
        event_generator(),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
