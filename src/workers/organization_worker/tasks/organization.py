"""Celery tasks for organisation operations.

These tasks are executed by the organization worker and publish status updates
to Redis for SSE streaming to the frontend.
"""

from typing import Any
from uuid import UUID

from dotenv import load_dotenv

from src.domain.models.master_tables.organisation_job_models import (
    OrganisationJobStatus,
)
from src.infra.database.factory import get_async_provider
from src.infra.messaging.organisation_job_status_publish import publish_org_job_status
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.admin.organisation_job_service import organisation_job_service
from src.service.orgnisation.organization_management import (
    organization_management_service,
)
from src.workers.organization_worker.celery_app import app

# Load environment variables
load_dotenv(override=True)

logger = get_logger("organization_tasks")


def _publish_status(
    job_id: str,
    status: OrganisationJobStatus,
    progress: int,
    status_message: str | None = None,
    result_data: dict[str, Any] | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
):
    """Helper to publish job status to Redis for SSE streaming."""
    publish_org_job_status(
        job_id=job_id,
        status=status.value,
        progress=progress,
        status_message=status_message,
        result_data=result_data,
        error_message=error_message,
        error_code=error_code,
    )


async def _update_job_in_db(
    session,
    job_id: str,
    status: OrganisationJobStatus,
    progress: int,
    status_message: str | None = None,
    result_data: dict[str, Any] | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
    organisation_id: UUID | None = None,
):
    """Update job status in database using provided session."""
    if organisation_id:
        await organisation_job_service.set_organisation_id(session, UUID(job_id), organisation_id)
    await organisation_job_service.update_job_status(
        session=session,
        job_id=UUID(job_id),
        status=status,
        progress=progress,
        status_message=status_message,
        result_data=result_data,
        error_message=error_message,
        error_code=error_code,
    )


@app.task(name="organization.create", bind=True, acks_late=True, max_retries=3)
def create_organization_task(
    self,
    admin_id: str,
    organisation_name: str,
    org_details: dict[str, Any],
    job_id: str | None = None,
) -> dict[str, Any]:
    """
    Celery task to create a new organization.

    Publishes status updates to Redis for SSE streaming if job_id is provided.

    Args:
        admin_id: UUID of admin creating the organisation
        organisation_name: Name for the new organisation
        org_details: Additional organisation details
        job_id: Optional job UUID for status tracking (new async pattern)
    """
    logger.info(f"Creating organization: {organisation_name} for admin: {admin_id}")

    async def _run_task():
        """Main async task execution."""
        provider = get_async_provider()

        async with provider.master_session() as session:
            try:
                # Publish initial status if job tracking enabled
                if job_id:
                    _publish_status(
                        job_id=job_id,
                        status=OrganisationJobStatus.IN_PROGRESS,
                        progress=10,
                        status_message="Starting organisation provisioning",
                    )
                    await _update_job_in_db(
                        session=session,
                        job_id=job_id,
                        status=OrganisationJobStatus.IN_PROGRESS,
                        progress=10,
                        status_message="Starting organisation provisioning",
                    )

                # Progress: 20% - Creating database records
                if job_id:
                    _publish_status(
                        job_id=job_id,
                        status=OrganisationJobStatus.IN_PROGRESS,
                        progress=20,
                        status_message="Creating organisation record",
                    )

                result = await organization_management_service.create_organization(
                    admin_id=UUID(admin_id),
                    organisation_name=organisation_name,
                    session=session,
                    **org_details,
                )

                # Progress: 80% - Finalizing
                if job_id:
                    _publish_status(
                        job_id=job_id,
                        status=OrganisationJobStatus.IN_PROGRESS,
                        progress=80,
                        status_message="Finalizing organisation setup",
                    )

                organization = result["organization"]
                logger.info(f"Successfully created organization: {organization.organisation_id}")

                response_data = {
                    "organisation_id": str(organization.organisation_id),
                    "organisation_name": organization.organisation_name,
                    "schema_name": result["schema_name"],
                    "created_at": organization.created_at.isoformat(),
                    "updated_at": organization.updated_at.isoformat(),
                }

                # Publish completion status
                if job_id:
                    _publish_status(
                        job_id=job_id,
                        status=OrganisationJobStatus.COMPLETED,
                        progress=100,
                        status_message="Organisation created successfully",
                        result_data=response_data,
                    )
                    await _update_job_in_db(
                        session=session,
                        job_id=job_id,
                        status=OrganisationJobStatus.COMPLETED,
                        progress=100,
                        status_message="Organisation created successfully",
                        result_data=response_data,
                        organisation_id=organization.organisation_id,
                    )

                return response_data

            except Exception as e:
                logger.error(
                    f"Failed to create organization {organisation_name}: {e}", exc_info=True
                )

                # Publish failure status
                if job_id:
                    error_msg = str(e)
                    _publish_status(
                        job_id=job_id,
                        status=OrganisationJobStatus.FAILED,
                        progress=0,
                        status_message="Organisation creation failed",
                        error_message=error_msg,
                        error_code="PROVISIONING_ERROR",
                    )
                    # Use a new session for error update since main session may be in bad state
                    async with provider.master_session() as error_session:
                        await _update_job_in_db(
                            session=error_session,
                            job_id=job_id,
                            status=OrganisationJobStatus.FAILED,
                            progress=0,
                            status_message="Organisation creation failed",
                            error_message=error_msg,
                            error_code="PROVISIONING_ERROR",
                        )
                raise

    return run_async(_run_task())
