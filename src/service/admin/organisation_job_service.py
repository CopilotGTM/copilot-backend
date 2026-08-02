"""Service for managing organisation jobs.

Handles CRUD operations for organisation jobs in the master database.
Jobs track async organisation operations (create, update, delete, migrate).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables.organisation_job_models import (
    OrganisationJob,
    OrganisationJobStatus,
    OrganisationJobType,
)
from src.infra.utils.logger import get_logger

logger = get_logger("organisation_job_service")


class OrganisationJobService:
    """Service for managing organisation job records."""

    async def create_job(
        self,
        session: AsyncSession,
        job_type: OrganisationJobType,
        admin_id: UUID,
        organisation_id: UUID | None = None,
        request_payload: dict[str, Any] | None = None,
    ) -> OrganisationJob:
        """Create a new organisation job record.

        Args:
            session: Async database session
            job_type: Type of job (CREATE, UPDATE, DELETE, MIGRATE)
            admin_id: ID of the admin user initiating the job
            organisation_id: Target organisation ID (None for CREATE)
            request_payload: Original request data for retry/audit

        Returns:
            Created OrganisationJob record
        """
        # CREATE retries must share the same target UUID; display names are no
        # longer unique and cannot safely identify partially provisioned rows.
        if job_type == OrganisationJobType.CREATE and organisation_id is None:
            organisation_id = uuid4()
        job = OrganisationJob(
            job_id=uuid4(),
            job_type=job_type,
            admin_id=admin_id,
            organisation_id=organisation_id,
            status=OrganisationJobStatus.PENDING,
            progress=0,
            request_payload=request_payload,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)

        logger.info(
            f"Created organisation job {job.job_id} (type={job_type.value}, "
            f"admin={admin_id}, org={organisation_id})"
        )
        return job

    async def get_job(
        self,
        session: AsyncSession,
        job_id: UUID,
    ) -> OrganisationJob | None:
        """Get a job by ID.

        Args:
            session: Async database session
            job_id: Job UUID

        Returns:
            OrganisationJob if found, None otherwise
        """
        result = await session.execute(
            select(OrganisationJob).where(col(OrganisationJob.job_id) == job_id)
        )
        return result.scalars().first()

    async def get_job_by_admin(
        self,
        session: AsyncSession,
        job_id: UUID,
        admin_id: UUID,
    ) -> OrganisationJob | None:
        """Get a job by ID, scoped to the admin who created it.

        Args:
            session: Async database session
            job_id: Job UUID
            admin_id: Admin user ID (for authorization)

        Returns:
            OrganisationJob if found and owned by admin, None otherwise
        """
        result = await session.execute(
            select(OrganisationJob).where(
                col(OrganisationJob.job_id) == job_id,
                col(OrganisationJob.admin_id) == admin_id,
            )
        )
        return result.scalars().first()

    async def list_jobs_by_admin(
        self,
        session: AsyncSession,
        admin_id: UUID,
        status: OrganisationJobStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[OrganisationJob], int]:
        """List jobs created by an admin with optional status filter.

        Args:
            session: Async database session
            admin_id: Admin user ID
            status: Optional status filter
            limit: Max results to return
            offset: Pagination offset

        Returns:
            Tuple of (jobs list, total count)
        """
        query = select(OrganisationJob).where(col(OrganisationJob.admin_id) == admin_id)
        if status:
            query = query.where(col(OrganisationJob.status) == status)

        # Get total count
        from sqlalchemy import func

        count_query = select(func.count()).select_from(query.subquery())
        count_result = await session.execute(count_query)
        total = count_result.scalar() or 0

        # Get paginated results
        query = query.order_by(col(OrganisationJob.created_at).desc())
        query = query.offset(offset).limit(limit)
        result = await session.execute(query)
        jobs = list(result.scalars().all())

        return jobs, total

    async def update_job_status(
        self,
        session: AsyncSession,
        job_id: UUID,
        status: OrganisationJobStatus,
        progress: int | None = None,
        status_message: str | None = None,
        result_data: dict[str, Any] | None = None,
        error_message: str | None = None,
        error_code: str | None = None,
    ) -> OrganisationJob | None:
        """Update job status and related fields.

        Args:
            session: Async database session
            job_id: Job UUID
            status: New status
            progress: Progress percentage (0-100)
            status_message: Human-readable status message
            result_data: Result data on completion
            error_message: Error message on failure
            error_code: Error code on failure

        Returns:
            Updated OrganisationJob if found, None otherwise
        """
        job = await self.get_job(session, job_id)
        if not job:
            logger.warning(f"Job {job_id} not found for status update")
            return None

        job.status = status
        job.updated_at = datetime.now(UTC)

        if progress is not None:
            job.progress = progress
        if status_message is not None:
            job.status_message = status_message
        if result_data is not None:
            job.result_data = result_data
        if error_message is not None:
            job.error_message = error_message
        if error_code is not None:
            job.error_code = error_code

        # Set completed_at for terminal states
        if status in (
            OrganisationJobStatus.COMPLETED,
            OrganisationJobStatus.FAILED,
            OrganisationJobStatus.CANCELLED,
        ):
            job.completed_at = datetime.now(UTC)

        await session.commit()
        await session.refresh(job)

        logger.info(f"Updated job {job_id}: status={status.value}, progress={job.progress}")
        return job

    async def increment_attempt(
        self,
        session: AsyncSession,
        job_id: UUID,
    ) -> OrganisationJob | None:
        """Increment job attempt count for retries.

        Args:
            session: Async database session
            job_id: Job UUID

        Returns:
            Updated OrganisationJob if found, None otherwise
        """
        job = await self.get_job(session, job_id)
        if not job:
            return None

        job.attempt_count += 1
        job.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(job)

        logger.info(f"Incremented attempt for job {job_id}: attempt={job.attempt_count}")
        return job

    async def set_organisation_id(
        self,
        session: AsyncSession,
        job_id: UUID,
        organisation_id: UUID,
    ) -> OrganisationJob | None:
        """Set the organisation_id after creation.

        For CREATE jobs, the organisation_id is not known until the org is created.

        Args:
            session: Async database session
            job_id: Job UUID
            organisation_id: Created organisation ID

        Returns:
            Updated OrganisationJob if found, None otherwise
        """
        job = await self.get_job(session, job_id)
        if not job:
            return None

        job.organisation_id = organisation_id
        job.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(job)

        logger.info(f"Set organisation_id for job {job_id}: org={organisation_id}")
        return job


# Singleton instance for convenience
organisation_job_service = OrganisationJobService()
