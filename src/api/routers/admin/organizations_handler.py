from typing import Optional, Dict, Any, List
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select
from sqlalchemy import or_, and_

from src.domain.models.master_tables import SuperAdmin, AdminRole
from src.domain.models.admin_org_models import (
    CreateOrganizationRequest,
    CreateOrganizationResponse,
    DeleteOrganizationResponse,
    PaginationOrganizations,
    UpdateOrganizationRequest,
    ListOrganizationResponse,
    UpdateOrganisationStatusRequest,
    UpdateOrganisationStatusResponse,
    PaginatedOrganisationActionHistoryResponse,
)
from src.domain.models.master_tables import (
    Organisation,
    OrganisationActionHistory,
    OrganisationEventType,
    OrganisationStatus,
)
from src.infra.utils.database import get_master_session
from src.infra.utils.logger import get_logger
from src.infra.utils.cursor import encode_cursor, decode_cursor
from src.service.orgnisation.organization_management import organization_management_service
from src.api.utils.http_response import Response
from src.infra.auth.admin_auth import admin_auth
from src.api.schemas.base_models import GenericResponse
from src.tasks.organization import create_organization_task

logger = get_logger("admin_organisations_handler")
router = APIRouter()


@router.post(
    "/create_organisation",
    response_model=GenericResponse[CreateOrganizationResponse],
    tags=["admin_organisations"],
)
async def create_organization(
    request: CreateOrganizationRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
):
    """
    Create a new organization with a dedicated schema, role, and database user.
    This endpoint is restricted to super admins.
    """
    try:
        current_user = current_admin.admin_id
        logger.info(f"Creating organization for admin: {current_user}")
        logger.info(f"Organization creation request for '{request.organisation_name}' by admin: {current_admin.email}")

        # Ensure the user has super admin role
        if current_admin.role not in [AdminRole.SUPER_ADMIN]:
            logger.warning(f"Unauthorized attempt to create organization by user: {current_user}. Super admin access required.")
            return Response.forbidden_error_response("Super admin access required")

        # details for the organization
        org_details = request.dict(exclude_unset=True, exclude={"organisation_name"})
        logger.info(f"Organization details: {org_details}")

        task_result = create_organization_task.delay(
            admin_id=str(current_user),
            organisation_name=request.organisation_name,
            org_details=org_details,
        ).get(timeout=300)  # 5-minute timeout for the synchronous task

        response_data = CreateOrganizationResponse(**task_result)
        
        logger.info(f"Successfully created organization: {response_data.organisation_name} with ID: {response_data.organisation_id}")
        return Response.success_response(message="Organization created successfully", data=response_data)

    except Exception as e:
        logger.error(
            f"Error creating organization '{request.organisation_name}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message=f"Failed to create organization - {e}", data={})


@router.get(
    "/list_organisations",
    response_model=GenericResponse[PaginationOrganizations],
    tags=["admin_organisations"],
)
async def list_organizations(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(
        None,
        description="Opaque cursor returned by the previous call (omit for first page)",
    ),
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
):
    """
    Retrieve a list of all organizations with pagination.
    This endpoint is restricted to super admins.
    """
    try:
        logger.info(f"Request to list organizations by admin: {current_admin.email}")

        # Ensure the user has super admin role
        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to list organizations by user: {current_admin.admin_id}.")
            return Response.forbidden_error_response("Super admin access required")

        filters = []
        if cursor:
            ts, org_id = decode_cursor(cursor)
            filters.append(
                or_(
                    Organisation.created_at < ts,
                    and_(
                        Organisation.created_at == ts,
                        Organisation.organisation_id < org_id,
                    ),
                )
            )

        stmt = (
            select(Organisation)
            .where(*filters)
            .order_by(Organisation.created_at.desc(), Organisation.organisation_id.desc())
            .limit(limit + 1)
        )

        organizations: List[Organisation] = master_session.exec(stmt).all()
        has_more = len(organizations) == limit + 1
        items = organizations[:limit]

        next_cursor = encode_cursor(items[-1].created_at, items[-1].organisation_id) if has_more else None
        
        response_data = PaginationOrganizations(organizations=items, cursor=next_cursor)
        
        logger.info(f"Successfully retrieved {len(items)} organizations.")
        return Response.success_response(message="Organizations retrieved successfully", data=response_data)

    except Exception as e:
        logger.error(
            f"Error listing organizations: {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message=f"Failed to list organizations - {e}", data={})


@router.get(
    "/{organisation_id}",
    response_model=GenericResponse[ListOrganizationResponse],
    tags=["admin_organisations"],
)
async def get_organization(
    organisation_id: UUID,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
):
    """
    Retrieve a single organization by its ID.
    This endpoint is restricted to super admins and admins.
    """
    try:
        logger.info(f"Request to get organization '{organisation_id}' by admin: {current_admin.email}")

        # Ensure the user has super admin or admin role
        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to get organization by user: {current_admin.admin_id}.")
            return Response.forbidden_error_response("Admin access required")

        # Fetch organization by ID
        statement = select(Organisation).where(Organisation.organisation_id == organisation_id)
        result = master_session.exec(statement)
        organization = result.first()

        if not organization:
            logger.warning(f"Organization with ID '{organisation_id}' not found.")
            return Response.not_found_error_response("Organization not found")

        response_data = ListOrganizationResponse.from_orm(organization)
        
        logger.info(f"Successfully retrieved organization: {organization.organisation_name} with ID: {organization.organisation_id}")
        return Response.success_response(message="Organization retrieved successfully", data=response_data)

    except Exception as e:
        logger.error(
            f"Error retrieving organization '{organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message=f"Failed to retrieve organization - {e}", data={})


@router.put(
    "/{organisation_id}/update",
    response_model=GenericResponse[ListOrganizationResponse],
    tags=["admin_organisations"],
)
async def update_organization(
    organisation_id: UUID,
    request: UpdateOrganizationRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
):
    """
    Update an organization's details.
    This endpoint is restricted to super admins.
    """
    try:
        logger.info(f"Update request for organization '{organisation_id}' by admin: {current_admin.email}")

        # Ensure the user has super admin role
        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            logger.warning(f"Unauthorized attempt to update organization by user: {current_admin.admin_id}.")
            return Response.forbidden_error_response("Super admin access required")

        update_data = request.dict(exclude_unset=True)
        if not update_data:
            return Response.bad_request_error_response("No update data provided")

        organization = organization_management_service.update_organization(
            session=master_session,
            admin_id=current_admin.admin_id,
            organisation_id=organisation_id,
            update_data=update_data,
        )

        logger.info(f"Successfully updated organization: {organization.organisation_name}")
        return Response.success_response(message="Organization updated successfully", data=ListOrganizationResponse.from_orm(organization))

    except ValueError as ve:
        logger.warning(f"Value error while updating organization: {ve}")
        return Response.not_found_error_response(message=str(ve))
    except Exception as e:
        logger.error(
            f"Error updating organization '{organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message="Failed to update organization", data={})


@router.put(
    "/{organisation_id}/status",
    response_model=GenericResponse[UpdateOrganisationStatusResponse],
    tags=["admin_organisations"],
)
async def update_organisation_status(
    organisation_id: UUID,
    request: UpdateOrganisationStatusRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
):
    """
    Update the status of an organization (e.g., approve, reject, suspend).
    This endpoint is restricted to super admins.
    """
    try:
        logger.info(f"Status update request for organization '{organisation_id}' to '{request.status}' by admin: {current_admin.email}")

        if current_admin.role not in [AdminRole.SUPER_ADMIN]:
            logger.warning(f"Unauthorized attempt to update organization status by user: {current_admin.admin_id}.")
            return Response.forbidden_error_response("Super admin access required")

        organization = organization_management_service.update_organisation_status(
            session=master_session,
            organisation_id=organisation_id,
            admin_id=current_admin.admin_id,
            new_status=request.status,
            note=request.note,
        )

        response_data = UpdateOrganisationStatusResponse(
            organisation_id=organization.organisation_id,
            status=organization.status,
            updated_at=organization.updated_at,
            approved_at=organization.approved_at,
            approved_by=organization.approved_by,
        )

        logger.info(f"Successfully updated status for organization: {organization.organisation_name} to {organization.status}")
        return Response.success_response(message=f"Organization status updated to '{organization.status.value}'", data=response_data)

    except ValueError as ve:
        logger.warning(f"Value error while updating organization status: {ve}")
        return Response.bad_request_error_response(message=str(ve))
    except Exception as e:
        logger.error(
            f"Error updating organization status for '{organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message="Failed to update organization status", data={})


@router.get(
    "/{organisation_id}/history",
    response_model=GenericResponse[PaginatedOrganisationActionHistoryResponse],
    tags=["admin_organisations"],
)
async def get_organisation_history(
    organisation_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None),
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
):
    """
    Retrieve the action history for a specific organization.
    """
    try:
        if current_admin.role not in [AdminRole.SUPER_ADMIN, AdminRole.ADMIN]:
            return Response.forbidden_error_response("Admin access required")

        history_items = organization_management_service.get_organization_history(
            session=master_session,
            organisation_id=organisation_id,
            limit=limit,
            cursor=cursor,
        )

        has_more = len(history_items) == limit + 1
        items = history_items[:limit]
        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more else None

        response_data = PaginatedOrganisationActionHistoryResponse(history=items, cursor=next_cursor)
        return Response.success_response(message="Organization history retrieved successfully", data=response_data)

    except Exception as e:
        logger.error(f"Error retrieving history for organization '{organisation_id}': {e}", exc_info=True)
        return Response.internal_server_error_response(message="Failed to retrieve organization history")


@router.get(
    "/all_organisations_history",
    response_model=GenericResponse[PaginatedOrganisationActionHistoryResponse],
    tags=["admin_organisations"],
)
async def get_all_organisation_history(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None),
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
):
    """
    Retrieve the action history for all organizations.
    """
    try:
        if current_admin.role not in [AdminRole.SUPER_ADMIN]:
            return Response.forbidden_error_response("Super admin access required")

        history_items = organization_management_service.get_all_organization_history(
            session=master_session,
            limit=limit,
            cursor=cursor,
        )

        has_more = len(history_items) == limit + 1
        items = history_items[:limit]
        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more else None

        response_data = PaginatedOrganisationActionHistoryResponse(history=items, cursor=next_cursor)
        return Response.success_response(message="All organization history retrieved successfully", data=response_data)

    except Exception as e:
        logger.error(f"Error retrieving all organization history: {e}", exc_info=True)
        return Response.internal_server_error_response(message="Failed to retrieve all organization history")


# @router.delete(
#     "/{organisation_id}/delete",
#     response_model=GenericResponse[DeleteOrganizationResponse],
#     tags=["admin_organisations"],
# )
# async def delete_organization(
#     organisation_id: UUID,
#     current_admin: SuperAdmin = Depends(admin_auth),
#     master_session: Session = Depends(lambda: next(lambda: Session(get_master_engine(echo=False)))),
# ):
#     """
#     Permanently delete an organization, its schema, role, and all associated data.
#     This is a destructive operation and cannot be undone.
#     This endpoint is restricted to super admins.
#     """
#     try:
#         logger.warning(f"Deletion request for organization '{organisation_id}' by admin: {current_admin.email}. This is a destructive operation.")

#         if current_admin.role not in [AdminRole.SUPER_ADMIN]:
#             logger.warning(f"Unauthorized attempt to delete organization by user: {current_admin.admin_id}.")
#             return Response.forbidden_error_response("Super admin access required")

#         deleted_organization = organization_management_service.delete_organization(
#             session=master_session,
#             organisation_id=organisation_id,
#             admin_id=current_admin.admin_id,
#         )

#         response_data = DeleteOrganizationResponse(
#             organisation_id=deleted_organization.organisation_id,
#             organisation_name=deleted_organization.organisation_name,
#             schema_name=deleted_organization.schema_name,
#         )

#         logger.info(f"Successfully deleted organization: {deleted_organization.organisation_name}")
#         return Response.success_response(message="Organization deleted successfully", data=response_data)

#     except ValueError as ve:
#         logger.warning(f"Value error while deleting organization: {ve}")
#         return Response.not_found_error_response(message=str(ve))
#     except Exception as e:
#         logger.error(
#             f"Error deleting organization '{organisation_id}': {e}",
#             exc_info=True,
#         )
#         return Response.internal_server_error_response(message="Failed to delete organization", data={})
