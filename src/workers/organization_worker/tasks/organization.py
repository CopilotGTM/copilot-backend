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
from src.infra.database.sessions import master_async_session_scope
from src.infra.messaging.organisation_job_status_publish import publish_org_job_status
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.admin.organisation_job_service import organisation_job_service
from src.service.orgnisation.kai_training_service import KaiTrainingService
from src.service.orgnisation.organization_management import (
    organization_management_service,
)
from src.workers.organization_worker.celery_app import app
from src.workers.organization_worker.client import send_organization_research_task

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
    job_id: str,
    status: OrganisationJobStatus,
    progress: int,
    status_message: str | None = None,
    result_data: dict[str, Any] | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
    organisation_id: UUID | None = None,
):
    """Update job status in database using a scoped session."""
    async with master_async_session_scope() as session:
        if organisation_id:
            await organisation_job_service.set_organisation_id(
                session, UUID(job_id), organisation_id
            )
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


@app.task(
    name="organization.create",
    bind=True,
    acks_late=True,
    max_retries=3,
    soft_time_limit=600,  # 10 minutes — migrations alone can take 4+ minutes
    time_limit=660,
)
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
        """Main async task execution.

        Each status update uses its own scoped session so no single
        connection is held across the long provisioning phase.
        """
        try:
            # DB-first: persist IN_PROGRESS before publishing to SSE
            if job_id:
                await _update_job_in_db(
                    job_id=job_id,
                    status=OrganisationJobStatus.IN_PROGRESS,
                    progress=10,
                    status_message="Starting organisation provisioning",
                )
                _publish_status(
                    job_id=job_id,
                    status=OrganisationJobStatus.IN_PROGRESS,
                    progress=10,
                    status_message="Starting organisation provisioning",
                )

            if job_id:
                _publish_status(
                    job_id=job_id,
                    status=OrganisationJobStatus.IN_PROGRESS,
                    progress=20,
                    status_message="Creating organisation record",
                )

            # create_organization manages its own sessions internally
            result = await organization_management_service.create_organization(
                admin_id=UUID(admin_id),
                organisation_name=organisation_name,
                **org_details,
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

            # DB-first: persist COMPLETED before publishing to SSE
            if job_id:
                await _update_job_in_db(
                    job_id=job_id,
                    status=OrganisationJobStatus.COMPLETED,
                    progress=100,
                    status_message="Organisation created successfully",
                    result_data=response_data,
                    organisation_id=organization.organisation_id,
                )
                _publish_status(
                    job_id=job_id,
                    status=OrganisationJobStatus.COMPLETED,
                    progress=100,
                    status_message="Organisation created successfully",
                    result_data=response_data,
                )

            # Research is best-effort and must never turn successful provisioning
            # into a failed organisation creation job.
            try:
                async with master_async_session_scope() as session:
                    _, should_enqueue = await KaiTrainingService(
                        session, organization.organisation_id
                    ).mark_pending()
                if should_enqueue:
                    await send_organization_research_task(str(organization.organisation_id))
            except Exception as research_exc:
                logger.exception(
                    "Unable to queue initial Kai Training research for %s",
                    organization.organisation_id,
                )
                try:
                    async with master_async_session_scope() as session:
                        await KaiTrainingService(session, organization.organisation_id).mark_failed(
                            str(research_exc)
                        )
                except Exception:
                    logger.exception(
                        "Unable to record initial Kai Training research failure for %s",
                        organization.organisation_id,
                    )

            return response_data

        except Exception as e:
            logger.error(
                f"Failed to create organization {organisation_name}: {e}",
                exc_info=True,
            )

            # DB-first: persist FAILED before publishing to SSE
            if job_id:
                error_msg = str(e)
                await _update_job_in_db(
                    job_id=job_id,
                    status=OrganisationJobStatus.FAILED,
                    progress=0,
                    status_message="Organisation creation failed",
                    error_message=error_msg,
                    error_code="PROVISIONING_ERROR",
                )
                _publish_status(
                    job_id=job_id,
                    status=OrganisationJobStatus.FAILED,
                    progress=0,
                    status_message="Organisation creation failed",
                    error_message=error_msg,
                    error_code="PROVISIONING_ERROR",
                )
            raise

    return run_async(_run_task())


@app.task(
    name="organization.research_kai_training",
    bind=True,
    acks_late=True,
    max_retries=2,
    soft_time_limit=180,
    time_limit=210,
)
def research_kai_training_task(self, organisation_id: str) -> dict[str, str]:
    """Research and persist Kai's organisation profile without holding DB sessions."""
    from src.service.orgnisation.kai_training_research import KaiTrainingResearchService

    org_uuid = UUID(organisation_id)
    try:
        run_async(KaiTrainingResearchService(org_uuid).run())
        return {"status": "completed", "organisation_id": organisation_id}
    except Exception as exc:
        logger.exception("Kai Training research failed for %s", organisation_id)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc

        error_message = str(exc)

        async def _mark_failed() -> None:
            async with master_async_session_scope() as session:
                await KaiTrainingService(session, org_uuid).mark_failed(error_message)

        run_async(_mark_failed())
        return {"status": "failed", "organisation_id": organisation_id}
