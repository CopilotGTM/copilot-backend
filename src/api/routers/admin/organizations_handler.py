from typing import Dict, Any, List
from fastapi import APIRouter, Depends
from sqlmodel import Session
from uuid import UUID

from src.domain.models.admin_models import SuperAdmin
from src.domain.models.admin_org_models import (
    CreateOrganizationRequest,
    CreateOrganizationResponse,
    ListOrganizationsResponse,
    OrganizationResponse,
    UpdateOrganizationRequest,
    DeleteOrganizationResponse,
    RunMigrationsRequest,
    RunMigrationsResponse,
    RunMasterMigrationsResponse,
)
from src.infra.utils.database import get_master_session
from src.infra.utils.logger import get_logger
from src.service.organization_management import organization_management_service
from src.api.utils.http_response import Response
from src.infra.auth.admin_auth import admin_auth

logger = get_logger("admin_organizations_handler")
admin_org_router = APIRouter()


@admin_org_router.post("/create_organisation")
async def create_organization(
    request: CreateOrganizationRequest,
    auth_data: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Create a new organization with schema, role, and admin user.
    Only super admins can create organizations.
    """
    try:
        # Check if current user is super admin
        logger.info(f"Creating organization: {request.organisation_name} by admin: {auth_data.admin_id}")
        current_user = auth_data.admin_id

        # Ensure the user has super admin role
        if not current_user or not auth_data.role or auth_data.role != "super_admin":
            logger.warning(f"Unauthorized attempt to create organization by user: {auth_data.admin_id}. Super admin access required.")
            return Response.forbidden_error_response("Super admin access required")

        # Validate request data
        logger.info(f"Preparing to create organization: {request.organisation_name}")
        result = organization_management_service.create_organization(
            organisation_name=request.organisation_name,
            session=master_session,
            **request.dict(exclude_unset=True, exclude={"organisation_name", "admin_email", "admin_first_name", "admin_last_name"}),
        )
        logger.info(f"Organization created successfully: {result['organization'].organisation_id}")
        return {"message": "Organization created successfully"}

    except Exception as e:
        logger.error(f"Error creating organization: {str(e)}")
        return Response.internal_server_error_response("Failed to create organization", data=str(e))