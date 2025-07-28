from typing import Optional, Dict, Any, List
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select
from sqlalchemy import or_, and_

from src.domain.models.master_db_models import SuperAdmin, AdminRole
from src.domain.models.admin_org_models import (
    CreateOrganizationRequest,
    CreateOrganizationResponse,
    ApproveOrganizationRequest,
    ApproveOrganizationResponse,
    PaginationOrganizations,
    UpdateOrganizationRequest,
    ListOrganizationResponse,
)
from src.domain.models.master_db_models import (
    Organisation,
    OrganisationActionHistory,
    OrganisationEventType,
    OrganisationStatus,
)
from src.infra.utils.app_state import fastapp
from src.infra.utils.logger import get_logger
from src.infra.utils.cursor import encode_cursor, decode_cursor
from src.service.organization_management import organization_management_service
from src.api.utils.http_response import Response
from src.infra.auth.admin_auth import admin_auth
from src.api.schemas.base_models import GenericResponse

logger = get_logger("admin_organizations_handler")
router = APIRouter()


@router.post(
    "/create_organisation",
    # response_model=GenericResponse[CreateOrganizationResponse],
    tags=["admin_organizations"],
)
async def create_organization(
    request: CreateOrganizationRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
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

        result = organization_management_service.create_organization(
            admin_id=current_user,
            organisation_name=request.organisation_name,
            session=master_session,
            **org_details,
        )

        organization = result["organization"]
        response_data = CreateOrganizationResponse(
            organisation_id=organization.organisation_id,
            organisation_name=organization.organisation_name,
            schema_name=result["schema_name"],
            created_at=organization.created_at,
            updated_at=organization.updated_at,
        )
        
        logger.info(f"Successfully created organization: {organization.organisation_name} with ID: {organization.organisation_id}")
        return Response.success_response(message="Organization created successfully", data=response_data)

    except Exception as e:
        logger.error(
            f"Error creating organization '{request.organisation_name}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message=f"Failed to create organization - {e}", data={})


@router.post(
    "/approve_organisation",
    response_model=GenericResponse[ApproveOrganizationResponse],
    tags=["admin_organizations"],
)
async def approve_organization(
    request: ApproveOrganizationRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
):
    """
    Approve a pending organization.
    This endpoint is restricted to super admins.
    """
    try:
        logger.info(f"Organization approval request for '{request.organisation_id}' by admin: {current_admin.email}")

        # Ensure the user has super admin role
        if current_admin.role not in [AdminRole.SUPER_ADMIN]:
            logger.warning(f"Unauthorized attempt to approve organization by user: {current_admin.admin_id}.")
            return Response.forbidden_error_response("Super admin access required")

        organization = organization_management_service.approve_organization(
            session=master_session,
            organisation_id=request.organisation_id,
            admin_id=current_admin.admin_id,
            note=request.note,
        )

        response_data = ApproveOrganizationResponse(
            organisation_id=organization.organisation_id,
            status=organization.status,
            approved_at=organization.approved_at,
            approved_by=organization.approved_by,
        )
        
        logger.info(f"Successfully approved organization: {organization.organisation_name} with ID: {organization.organisation_id}")
        return Response.success_response(message="Organization approved successfully", data=response_data)

    except ValueError as ve:
        logger.warning(f"Value error while approving organization: {ve}")
        return Response.not_found_error_response(message=str(ve))
    except Exception as e:
        logger.error(
            f"Error approving organization '{request.organisation_id}': {e}",
            exc_info=True,
        )
        return Response.internal_server_error_response(message="Failed to approve organization", data={})


@router.get(
    "/list_organisations",
    response_model=GenericResponse[PaginationOrganizations],
    tags=["admin_organizations"],
)
async def list_organizations(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(
        None,
        description="Opaque cursor returned by the previous call (omit for first page)",
    ),
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
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

@router.put(
    "/update_organisation/{organisation_id}",
    response_model=GenericResponse[ListOrganizationResponse],
    tags=["admin_organizations"],
)
async def update_organization(
    organisation_id: UUID,
    request: UpdateOrganizationRequest,
    current_admin: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(lambda: next(fastapp.state.master_session())),
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