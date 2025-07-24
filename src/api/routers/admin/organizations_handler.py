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


@admin_org_router.post("/create_organisation", response_model=CreateOrganizationResponse)
async def create_organization(
    request: CreateOrganizationRequest,
    auth_data: SuperAdmin = Depends(admin_auth),
    master_session: Session = Depends(get_master_session),
) -> CreateOrganizationResponse:
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
            admin_email=request.admin_email,
            admin_first_name=request.admin_first_name,
            admin_last_name=request.admin_last_name,
            session=master_session,
            **request.dict(exclude_unset=True, exclude={"organisation_name", "admin_email", "admin_first_name", "admin_last_name"}),
        )

        organization = result["organization"]
        admin_user = result["admin_user"]

        logger.info(f"Successfully created organization: {organization.organisation_id} with admin user: {admin_user.user_id}")

        return CreateOrganizationResponse(
            organization=organization,
            admin_user=admin_user
        )

    except Exception as e:
        logger.error(f"Error creating organization: {str(e)}")
        return Response.internal_server_error_response("Failed to create organization")


@admin_org_router.get("/list")
async def list_organizations(
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    List all organizations.
    Only admins can view organizations.
    """
    try:
        # Check if current user is admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Admin access required")

        organizations = organization_management_service.list_organizations(master_session)

        org_list = []
        for org in organizations:
            org_list.append(
                {
                    "organisation_id": str(org.organisation_id),
                    "organisation_name": org.organisation_name,
                    "schema_name": org.schema_name,
                    "role_name": org.role_name,
                    "is_active": org.is_active,
                    "country": org.country,
                    "sales_team_size": org.sales_team_size,
                    "created_at": org.created_at.isoformat(),
                    "updated_at": org.updated_at.isoformat(),
                }
            )

        return Response.success_response("Organizations retrieved successfully", {"organizations": org_list})

    except Exception as e:
        logger.error(f"Error listing organizations: {str(e)}")
        return Response.internal_server_error_response("Failed to list organizations")


@admin_org_router.get("/{organisation_id}")
async def get_organization(
    organisation_id: UUID,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Get organization details by ID.
    Only admins can view organization details.
    """
    try:
        # Check if current user is admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Admin access required")

        organization = organization_management_service.get_organization_by_id(organisation_id, master_session)

        if not organization:
            return Response.not_found_error_response("Organization not found")

        return Response.success_response(
            "Organization retrieved successfully",
            {
                "organisation_id": str(organization.organisation_id),
                "organisation_name": organization.organisation_name,
                "schema_name": organization.schema_name,
                "role_name": organization.role_name,
                "is_active": organization.is_active,
                "country": organization.country,
                "sales_team_size": organization.sales_team_size,
                "company_address": organization.company_address,
                "billing_address": organization.billing_address,
                "linked_urls": organization.linked_urls,
                "website_urls": organization.website_urls,
                "summary": organization.summary,
                "logo_url": organization.logo_url,
                "created_at": organization.created_at.isoformat(),
                "updated_at": organization.updated_at.isoformat(),
            },
        )

    except Exception as e:
        logger.error(f"Error getting organization: {str(e)}")
        return Response.internal_server_error_response("Failed to get organization")


@admin_org_router.put("/{organisation_id}")
async def update_organization(
    organisation_id: UUID,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
    organisation_name: Optional[str] = None,
    country: Optional[str] = None,
    sales_team_size: Optional[int] = None,
    company_address: Optional[str] = None,
    billing_address: Optional[str] = None,
    linked_urls: Optional[str] = None,
    website_urls: Optional[str] = None,
    summary: Optional[str] = None,
    logo_url: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Update organization details.
    Only admins can update organizations.
    """
    try:
        # Check if current user is admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Admin access required")

        # Prepare updates
        updates = {}
        if organisation_name is not None:
            updates["organisation_name"] = organisation_name
        if country is not None:
            updates["country"] = country
        if sales_team_size is not None:
            updates["sales_team_size"] = sales_team_size
        if company_address is not None:
            updates["company_address"] = company_address
        if billing_address is not None:
            updates["billing_address"] = billing_address
        if linked_urls is not None:
            updates["linked_urls"] = linked_urls
        if website_urls is not None:
            updates["website_urls"] = website_urls
        if summary is not None:
            updates["summary"] = summary
        if logo_url is not None:
            updates["logo_url"] = logo_url
        if is_active is not None:
            updates["is_active"] = is_active

        if not updates:
            return Response.bad_request_error_response("No updates provided")

        # Update organization
        organization = organization_management_service.update_organization(organisation_id, master_session, **updates)

        if not organization:
            return Response.not_found_error_response("Organization not found")

        logger.info(f"Updated organization: {organisation_id} by admin: {current_user.user_id}")

        return Response.success_response(
            "Organization updated successfully",
            {
                "organisation_id": str(organization.organisation_id),
                "organisation_name": organization.organisation_name,
                "schema_name": organization.schema_name,
                "role_name": organization.role_name,
                "is_active": organization.is_active,
                "updated_at": organization.updated_at.isoformat(),
            },
        )

    except Exception as e:
        logger.error(f"Error updating organization: {str(e)}")
        return Response.internal_server_error_response("Failed to update organization")


@admin_org_router.delete("/{organisation_id}")
async def delete_organization(
    organisation_id: UUID,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Delete organization and its schema.
    Only super admins can delete organizations.
    """
    try:
        # Check if current user is super admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Super admin access required")

        # Get organization details for confirmation
        organization = organization_management_service.get_organization_by_id(organisation_id, master_session)

        if not organization:
            return Response.not_found_error_response("Organization not found")

        # Delete organization
        success = organization_management_service.delete_organization(organisation_id, master_session)

        if not success:
            return Response.internal_server_error_response("Failed to delete organization")

        logger.info(f"Deleted organization: {organisation_id} by admin: {current_user.user_id}")

        return Response.success_response(
            "Organization deleted successfully",
            {
                "organisation_id": str(organisation_id),
                "organisation_name": organization.organisation_name,
                "schema_name": organization.schema_name,
            },
        )

    except Exception as e:
        logger.error(f"Error deleting organization: {str(e)}")
        return Response.internal_server_error_response("Failed to delete organization")


@admin_org_router.post("/{organisation_id}/migrate")
async def run_organization_migrations(
    organisation_id: UUID,
    target_version: str = "head",
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Run migrations for a specific organization.
    Only admins can run migrations.
    """
    try:
        # Check if current user is admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Admin access required")

        # Get organization
        organization = organization_management_service.get_organization_by_id(organisation_id, master_session)

        if not organization:
            return Response.not_found_error_response("Organization not found")

        if not organization.schema_name:
            return Response.bad_request_error_response("Organization has no schema")

        # Run migrations
        organization_management_service.run_organization_migrations(organization.schema_name, target_version)

        logger.info(f"Ran migrations for organization: {organisation_id} by admin: {current_user.user_id}")

        return Response.success_response(
            "Migrations completed successfully",
            {
                "organisation_id": str(organisation_id),
                "schema_name": organization.schema_name,
                "target_version": target_version,
            },
        )

    except Exception as e:
        logger.error(f"Error running migrations: {str(e)}")
        return Response.internal_server_error_response("Failed to run migrations")


@admin_org_router.post("/migrate/master")
async def run_master_migrations(
    target_version: str = "head",
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Run master migrations (organizations, users, etc.).
    Only super admins can run master migrations.
    """
    try:
        # Check if current user is super admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Super admin access required")

        # Run master migrations
        organization_management_service.run_master_migrations(target_version)

        logger.info(f"Ran master migrations by admin: {current_user.user_id}")

        return Response.success_response(
            "Master migrations completed successfully",
            {"target_version": target_version, "admin_user_id": str(current_user.user_id)},
        )

    except Exception as e:
        logger.error(f"Error running master migrations: {str(e)}")
        return Response.internal_server_error_response("Failed to run master migrations")
