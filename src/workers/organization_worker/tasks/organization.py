import asyncio
from typing import Any
from uuid import UUID

from dotenv import load_dotenv

from src.infra.database.factory import get_async_provider
from src.infra.utils.logger import get_logger
from src.service.orgnisation.organization_management import (
    organization_management_service,
)
from src.workers.organization_worker.celery_app import app

# Load environment variables
load_dotenv(override=True)

logger = get_logger("organization_tasks")


@app.task(name="organization.create", bind=True, acks_late=True, max_retries=3)
def create_organization_task(
    self, admin_id: str, organisation_name: str, org_details: dict[str, Any]
) -> dict[str, Any]:
    """
    Celery task to create a new organization.
    """
    logger.info(f"Creating organization: {organisation_name} for admin: {admin_id}")

    try:
        provider = get_async_provider()

        async def main():
            async with provider.master_session() as session:
                return await organization_management_service.create_organization(
                    admin_id=UUID(admin_id),
                    organisation_name=organisation_name,
                    session=session,
                    **org_details,
                )

        result = asyncio.run(main())
        organization = result["organization"]

        logger.info(f"Successfully created organization: {organization.organisation_id}")

        return {
            "organisation_id": str(organization.organisation_id),
            "organisation_name": organization.organisation_name,
            "schema_name": result["schema_name"],
            "created_at": organization.created_at.isoformat(),
            "updated_at": organization.updated_at.isoformat(),
        }
    except Exception as e:
        logger.error(f"Failed to create organization {organisation_name}: {e}", exc_info=True)
        raise
