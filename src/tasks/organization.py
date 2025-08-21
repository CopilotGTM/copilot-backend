from src.celery_app import celery
from src.service.organization_management import organization_management_service
from src.infra.utils.database import get_master_engine
from sqlalchemy import text
from sqlmodel import Session
from uuid import UUID
from typing import Dict, Any

@celery.task(name="organization.create")
def create_organization_task(admin_id: str, organisation_name: str, org_details: Dict[str, Any]) -> Dict[str, Any]:
    """
    Celery task to create a new organization.
    """
    with Session(get_master_engine(echo=False)) as session:
        try:
            session.execute(text('RESET ROLE'))
            session.execute(text('SET search_path TO public'))
        except Exception:
            pass
        result = organization_management_service.create_organization(
            admin_id=UUID(admin_id),
            organisation_name=organisation_name,
            session=session,
            **org_details,
        )
        organization = result["organization"]
        return {
            "organisation_id": str(organization.organisation_id),
            "organisation_name": organization.organisation_name,
            "schema_name": result["schema_name"],
            "created_at": organization.created_at.isoformat(),
            "updated_at": organization.updated_at.isoformat(),
        }
