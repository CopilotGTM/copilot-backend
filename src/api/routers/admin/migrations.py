from typing import Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session
import subprocess
from pathlib import Path

from src.domain.models.authentication import Authentication
from src.infra.utils.database import get_master_session
from src.infra.utils.logger import get_logger
from src.service.organization_management import organization_management_service
from src.api.utils.http_response import Response

auth = Authentication()
logger = get_logger()
migration_router = APIRouter()


@migration_router.get("/check")
async def check_migration_status(
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Check if migrations need to be run.
    Only admins can check migration status.
    """
    try:
        # Check if current user is admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Admin access required")
        
        # Get project root
        project_root = Path(__file__).parent.parent.parent.parent.parent
        
        # Check master migrations
        master_config = project_root / "migrations" / "master_migrations" / "alembic.ini"
        org_config = project_root / "migrations" / "org_migrations" / "alembic.ini"
        
        master_needs_migration = False
        org_needs_migration = False
        
        try:
            # Check master migrations
            result = subprocess.run(
                ["alembic", "-c", str(master_config), "current"],
                capture_output=True,
                text=True,
                cwd=project_root
            )
            current_master = None
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                for line in lines:
                    if line.strip():
                        current_master = line.strip().split()[0]
                        break
            
            result = subprocess.run(
                ["alembic", "-c", str(master_config), "heads"],
                capture_output=True,
                text=True,
                cwd=project_root
            )
            latest_master = None
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                for line in lines:
                    if line.strip():
                        latest_master = line.strip().split()[0]
                        break
            
            master_needs_migration = current_master != latest_master
            
        except Exception as e:
            logger.error(f"Error checking master migrations: {e}")
            master_needs_migration = True
        
        # Check organization migrations (simplified check)
        try:
            # Get all organizations
            organizations = organization_management_service.list_organizations(master_session)
            org_count = len(organizations)
            org_needs_migration = org_count > 0  # Simplified - could be more sophisticated
        except Exception as e:
            logger.error(f"Error checking org migrations: {e}")
            org_needs_migration = True
        
        needs_migration = master_needs_migration or org_needs_migration
        
        return Response.success_response(
            "Migration status checked successfully",
            {
                "needs_migration": needs_migration,
                "master_migrations": {
                    "needed": master_needs_migration,
                    "current": current_master,
                    "latest": latest_master
                },
                "organization_migrations": {
                    "needed": org_needs_migration,
                    "organization_count": org_count
                }
            }
        )
        
    except Exception as e:
        logger.error(f"Error checking migration status: {str(e)}")
        return Response.internal_server_error_response("Failed to check migration status")


@migration_router.post("/run/master")
async def run_master_migrations(
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Run master migrations.
    Only super admins can run master migrations.
    """
    try:
        # Check if current user is super admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Super admin access required")
        
        # Run master migrations
        organization_management_service.run_master_migrations()
        
        logger.info(f"Master migrations run by admin: {current_user.user_id}")
        
        return Response.success_response(
            "Master migrations completed successfully",
            {
                "admin_user_id": str(current_user.user_id),
                "migration_type": "master"
            }
        )
        
    except Exception as e:
        logger.error(f"Error running master migrations: {str(e)}")
        return Response.internal_server_error_response("Failed to run master migrations")


@migration_router.post("/run/organizations")
async def run_organization_migrations(
    org_ids: List[str] = None,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Run organization migrations.
    Only admins can run organization migrations.
    """
    try:
        # Check if current user is admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Admin access required")
        
        # Run organization migrations
        if org_ids:
            for org_id in org_ids:
                organization = organization_management_service.get_organization_by_id(
                    org_id, master_session
                )
                if organization and organization.schema_name:
                    organization_management_service.run_organization_migrations(
                        organization.schema_name
                    )
        else:
            # Run for all organizations
            organizations = organization_management_service.list_organizations(master_session)
            for org in organizations:
                if org.schema_name:
                    organization_management_service.run_organization_migrations(
                        org.schema_name
                    )
        
        logger.info(f"Organization migrations run by admin: {current_user.user_id}")
        
        return Response.success_response(
            "Organization migrations completed successfully",
            {
                "admin_user_id": str(current_user.user_id),
                "migration_type": "organization",
                "target_organizations": org_ids or "all"
            }
        )
        
    except Exception as e:
        logger.error(f"Error running organization migrations: {str(e)}")
        return Response.internal_server_error_response("Failed to run organization migrations")


@migration_router.post("/run/all")
async def run_all_migrations(
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Run all migrations (master + organization).
    Only super admins can run all migrations.
    """
    try:
        # Check if current user is super admin
        current_user = auth_data.get("user")
        if not current_user or not current_user.is_admin:
            return Response.forbidden_error_response("Super admin access required")
        
        # Run master migrations first
        organization_management_service.run_master_migrations()
        
        # Run organization migrations
        organizations = organization_management_service.list_organizations(master_session)
        for org in organizations:
            if org.schema_name:
                organization_management_service.run_organization_migrations(
                    org.schema_name
                )
        
        logger.info(f"All migrations run by admin: {current_user.user_id}")
        
        return Response.success_response(
            "All migrations completed successfully",
            {
                "admin_user_id": str(current_user.user_id),
                "migration_type": "all",
                "organization_count": len(organizations)
            }
        )
        
    except Exception as e:
        logger.error(f"Error running all migrations: {str(e)}")
        return Response.internal_server_error_response("Failed to run all migrations") 