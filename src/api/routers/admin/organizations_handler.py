from fastapi import APIRouter, Depends
from sqlmodel import Session

from src.domain.models.master_db_models import SuperAdmin
from src.domain.models.admin_org_models import (
    CreateOrganizationRequest,
    CreateOrganizationResponse,
)
from src.infra.utils.app_state import fastapp
from src.infra.utils.logger import get_logger
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
        if not current_user or not current_admin.role or current_admin.role != "super_admin":
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
        return Response.internal_server_error_response(message="Failed to create organization", data={})