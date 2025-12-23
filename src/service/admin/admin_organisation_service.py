"""Admin Organisation Service - Service layer with session ownership.

This service follows the session dependency pattern from service-architecture-base.md:
- Session is passed to __init__ and owned by the service
- close() must be called when done (use dependency injection with try/finally)
- All business logic and database operations are in the service
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.admin_job_models import (
    OrganisationJobListResponse,
    OrganisationJobResponse,
)
from src.domain.models.admin_org_models import (
    ActiveJobInfo,
    ListOrganizationResponse,
    OrganisationActionHistoryResponse,
    PaginatedOrganisationActionHistoryResponse,
    PaginationOrganizations,
    UpdateOrganisationStatusResponse,
)
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.master_tables.organisation_job_models import (
    OrganisationJob,
    OrganisationJobStatus,
    OrganisationJobType,
)
from src.domain.models.master_tables.super_admin_models import SuperAdmin
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger
from src.service.orgnisation.organization_management import (
    organization_management_service,
)

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------


class AdminOrganisationServiceError(Exception):
    """Base exception for AdminOrganisationService."""

    def __init__(self, message: str, error_code: str | None = None):
        self.message = message
        self.error_code = error_code
        super().__init__(message)


class OrganisationNotFoundError(AdminOrganisationServiceError):
    """Raised when organisation is not found."""

    pass


class JobNotFoundError(AdminOrganisationServiceError):
    """Raised when job is not found."""

    pass


class NoUpdateDataError(AdminOrganisationServiceError):
    """Raised when no update data is provided."""

    pass


# -----------------------------------------------------------------------------
# Result Classes
# -----------------------------------------------------------------------------


class CreateJobResult:
    """Result for job creation."""

    def __init__(self, job: OrganisationJob):
        self.job_id = job.job_id
        self.job_type = job.job_type
        self.status = job.status


# -----------------------------------------------------------------------------
# Service
# -----------------------------------------------------------------------------


class AdminOrganisationService:
    """Service for managing organisations with session ownership.

    This service follows the session ownership pattern:
    - Session is passed to __init__ and owned by the service
    - close() must be called when done (use dependency injection with try/finally)
    """

    def __init__(self, session: AsyncSession):
        """Initialize the service with a database session.

        Args:
            session: AsyncSession for database operations
        """
        self._session = session

    async def close(self) -> None:
        """Close the database session.

        Should be called via dependency injection cleanup.
        """
        try:
            await self._session.close()
        except Exception:
            logger.debug("AdminOrganisationService session close failed", exc_info=True)

    # -------------------------------------------------------------------------
    # Organisation CRUD Operations
    # -------------------------------------------------------------------------

    async def list_organisations(
        self,
        limit: int,
        cursor: str | None,
    ) -> PaginationOrganizations:
        """List organisations with pagination, including active job info.

        Args:
            limit: Maximum number of organisations to return
            cursor: Pagination cursor

        Returns:
            PaginationOrganizations with organisations (including active jobs) and next cursor
        """
        filters = []
        if cursor:
            ts, org_id = decode_cursor(cursor)
            filters.append(
                or_(
                    col(Organisation.created_at) < ts,
                    and_(
                        col(Organisation.created_at) == ts,
                        col(Organisation.organisation_id) < org_id,
                    ),
                )
            )

        stmt = (
            select(Organisation)
            .where(*filters)
            .order_by(
                col(Organisation.created_at).desc(),
                col(Organisation.organisation_id).desc(),
            )
            .limit(limit + 1)
        )

        result = await self._session.execute(stmt)
        organizations = list(result.scalars().all())

        has_more = len(organizations) == limit + 1
        items = organizations[:limit]

        # Fetch active jobs for all organisations in one query
        org_ids = [org.organisation_id for org in items]
        active_jobs_map = await self._get_active_jobs_for_orgs(org_ids)

        # Build response with active job info
        org_responses = []
        for org in items:
            org_response = ListOrganizationResponse.model_validate(org)
            if org.organisation_id in active_jobs_map:
                org_response.active_job = active_jobs_map[org.organisation_id]
            org_responses.append(org_response)

        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].organisation_id) if has_more else None
        )

        return PaginationOrganizations(organizations=org_responses, cursor=next_cursor)

    async def _get_active_jobs_for_orgs(self, org_ids: list[UUID]) -> dict[UUID, ActiveJobInfo]:
        """Fetch active (pending/in_progress) jobs for given organisation IDs.

        Args:
            org_ids: List of organisation UUIDs

        Returns:
            Dict mapping org_id to ActiveJobInfo for orgs with active jobs
        """
        if not org_ids:
            return {}

        # Get active jobs for these orgs (pending or in_progress)
        active_statuses = [OrganisationJobStatus.PENDING, OrganisationJobStatus.IN_PROGRESS]
        stmt = (
            select(OrganisationJob)
            .where(
                col(OrganisationJob.organisation_id).in_(org_ids),
                col(OrganisationJob.status).in_(active_statuses),
            )
            .order_by(col(OrganisationJob.created_at).desc())
        )

        result = await self._session.execute(stmt)
        jobs = result.scalars().all()

        # Build map (only keep latest job per org if multiple)
        jobs_map: dict[UUID, ActiveJobInfo] = {}
        for job in jobs:
            if job.organisation_id and job.organisation_id not in jobs_map:
                jobs_map[job.organisation_id] = ActiveJobInfo(
                    job_id=job.job_id,
                    job_type=job.job_type,
                    status=job.status,
                    progress=job.progress,
                    status_message=job.status_message,
                    stream_url=f"/admin/organisations/jobs/{job.job_id}/stream",
                    created_at=job.created_at,
                )

        return jobs_map

    async def get_organisation(self, organisation_id: UUID) -> ListOrganizationResponse:
        """Get an organisation by ID.

        Args:
            organisation_id: Organisation's UUID

        Returns:
            ListOrganizationResponse with organisation data

        Raises:
            OrganisationNotFoundError: If organisation doesn't exist
        """
        statement = select(Organisation).where(
            col(Organisation.organisation_id) == organisation_id,
        )
        result = await self._session.execute(statement)
        organization = result.scalars().first()

        if not organization:
            raise OrganisationNotFoundError(
                "Organization not found",
                error_code="ORGANIZATION_NOT_FOUND",
            )

        return ListOrganizationResponse.from_orm(organization)

    async def update_organisation(
        self,
        organisation_id: UUID,
        admin_id: UUID,
        update_data: dict[str, Any],
    ) -> ListOrganizationResponse:
        """Update an organisation's details.

        Args:
            organisation_id: Organisation's UUID
            admin_id: Admin performing the update
            update_data: Dictionary of fields to update

        Returns:
            ListOrganizationResponse with updated organisation data

        Raises:
            NoUpdateDataError: If no update data provided
            OrganisationNotFoundError: If organisation doesn't exist
        """
        if not update_data:
            raise NoUpdateDataError(
                "No update data provided",
                error_code="NO_UPDATE_DATA",
            )

        try:
            organization = await organization_management_service.update_organization(
                session=self._session,
                admin_id=admin_id,
                organisation_id=organisation_id,
                update_data=update_data,
            )
            return ListOrganizationResponse.from_orm(organization)
        except ValueError as e:
            if "not found" in str(e).lower():
                raise OrganisationNotFoundError(
                    str(e), error_code="ORGANIZATION_NOT_FOUND"
                ) from None
            raise

    async def update_organisation_status(
        self,
        organisation_id: UUID,
        admin_id: UUID,
        new_status: OrganisationStatus,
        note: str | None,
    ) -> UpdateOrganisationStatusResponse:
        """Update an organisation's status.

        Args:
            organisation_id: Organisation's UUID
            admin_id: Admin performing the update
            new_status: New status to set
            note: Optional note for history

        Returns:
            UpdateOrganisationStatusResponse with updated status info

        Raises:
            OrganisationNotFoundError: If organisation doesn't exist
        """
        try:
            organization = await organization_management_service.update_organisation_status(
                session=self._session,
                organisation_id=organisation_id,
                admin_id=admin_id,
                new_status=new_status,
                note=note,
            )
            return UpdateOrganisationStatusResponse(
                organisation_id=organization.organisation_id,
                status=organization.status,
                updated_at=organization.updated_at,
                approved_at=organization.approved_at,
                approved_by=organization.approved_by,
            )
        except ValueError as e:
            if "not found" in str(e).lower():
                raise OrganisationNotFoundError(
                    str(e), error_code="ORGANIZATION_NOT_FOUND"
                ) from None
            raise

    # -------------------------------------------------------------------------
    # History Operations
    # -------------------------------------------------------------------------

    async def get_organisation_history(
        self,
        organisation_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> PaginatedOrganisationActionHistoryResponse:
        """Get action history for a specific organisation.

        Args:
            organisation_id: Organisation's UUID
            limit: Maximum number of records to return
            cursor: Pagination cursor

        Returns:
            PaginatedOrganisationActionHistoryResponse with history and next cursor
        """
        history_items = await organization_management_service.get_organization_history(
            session=self._session,
            organisation_id=organisation_id,
            limit=limit,
            cursor=cursor,
        )

        has_more = len(history_items) == limit + 1
        items = history_items[:limit]
        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more else None

        org_map: dict[UUID, Organisation] = {}
        admin_map: dict[UUID, SuperAdmin] = {}
        org_ids = {h.organisation_id for h in items}
        if org_ids:
            org_table = cast(Any, Organisation).__table__
            org_result = await self._session.execute(
                select(Organisation).where(org_table.c.organisation_id.in_(org_ids))
            )
            orgs = org_result.scalars().all()
            org_map = {o.organisation_id: o for o in orgs}

        admin_ids = {h.actor_admin_id for h in items if h.actor_admin_id is not None}
        if admin_ids:
            admin_table = cast(Any, SuperAdmin).__table__
            admin_result = await self._session.execute(
                select(SuperAdmin).where(admin_table.c.admin_id.in_(admin_ids))
            )
            admins = admin_result.scalars().all()
            admin_map = {a.admin_id: a for a in admins}

        history_responses: list[OrganisationActionHistoryResponse] = []
        for h in items:
            org = org_map.get(h.organisation_id)
            actor = admin_map.get(h.actor_admin_id) if h.actor_admin_id is not None else None
            org_name = org.organisation_name if org is not None else None
            org_status = org.status if org is not None else None
            history_responses.append(
                OrganisationActionHistoryResponse(
                    id=h.id,
                    organisation_id=h.organisation_id,
                    organisation_name=org_name,
                    organisation_status=org_status,
                    action=h.action.value,
                    note=h.note,
                    details=h.details,
                    created_at=h.created_at,
                    actor_admin_id=h.actor_admin_id,
                    actor_admin_name=(f"{actor.first_name} {actor.last_name}" if actor else None),
                    actor_admin_email=(str(actor.email) if actor else None),
                )
            )

        return PaginatedOrganisationActionHistoryResponse(
            history=history_responses,
            cursor=next_cursor,
        )

    async def get_all_organisations_history(
        self,
        limit: int,
        cursor: str | None,
    ) -> PaginatedOrganisationActionHistoryResponse:
        """Get action history for all organisations.

        Args:
            limit: Maximum number of records to return
            cursor: Pagination cursor

        Returns:
            PaginatedOrganisationActionHistoryResponse with history and next cursor
        """
        history_items = await organization_management_service.get_all_organization_history(
            session=self._session,
            limit=limit,
            cursor=cursor,
        )

        has_more = len(history_items) == limit + 1
        items = history_items[:limit]
        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more else None

        org_map: dict[UUID, Organisation] = {}
        admin_map: dict[UUID, SuperAdmin] = {}
        org_ids = {h.organisation_id for h in items}
        if org_ids:
            org_table = cast(Any, Organisation).__table__
            org_result = await self._session.execute(
                select(Organisation).where(org_table.c.organisation_id.in_(org_ids))
            )
            orgs = org_result.scalars().all()
            org_map = {o.organisation_id: o for o in orgs}

        admin_ids = {h.actor_admin_id for h in items if h.actor_admin_id is not None}
        if admin_ids:
            admin_table = cast(Any, SuperAdmin).__table__
            admin_result = await self._session.execute(
                select(SuperAdmin).where(admin_table.c.admin_id.in_(admin_ids))
            )
            admins = admin_result.scalars().all()
            admin_map = {a.admin_id: a for a in admins}

        history_responses: list[OrganisationActionHistoryResponse] = []
        for h in items:
            org = org_map.get(h.organisation_id)
            actor = admin_map.get(h.actor_admin_id) if h.actor_admin_id is not None else None
            org_name = org.organisation_name if org is not None else None
            org_status = org.status if org is not None else None
            history_responses.append(
                OrganisationActionHistoryResponse(
                    id=h.id,
                    organisation_id=h.organisation_id,
                    organisation_name=org_name,
                    organisation_status=org_status,
                    action=h.action.value,
                    note=h.note,
                    details=h.details,
                    created_at=h.created_at,
                    actor_admin_id=h.actor_admin_id,
                    actor_admin_name=(f"{actor.first_name} {actor.last_name}" if actor else None),
                    actor_admin_email=(str(actor.email) if actor else None),
                )
            )

        return PaginatedOrganisationActionHistoryResponse(
            history=history_responses,
            cursor=next_cursor,
        )

    # -------------------------------------------------------------------------
    # Job Operations
    # -------------------------------------------------------------------------

    async def create_job(
        self,
        job_type: OrganisationJobType,
        admin_id: UUID,
        organisation_id: UUID | None,
        request_payload: dict[str, Any],
    ) -> CreateJobResult:
        """Create a new organisation job.

        Args:
            job_type: Type of job (CREATE, UPDATE, etc.)
            admin_id: Admin creating the job
            organisation_id: Organisation ID (if known)
            request_payload: Job request data

        Returns:
            CreateJobResult with job info
        """
        from src.service.admin.organisation_job_service import organisation_job_service

        job = await organisation_job_service.create_job(
            session=self._session,
            job_type=job_type,
            admin_id=admin_id,
            organisation_id=organisation_id,
            request_payload=request_payload,
        )
        return CreateJobResult(job)

    async def get_job(
        self,
        job_id: UUID,
        admin_id: UUID,
    ) -> OrganisationJobResponse:
        """Get a job by ID for a specific admin.

        Args:
            job_id: Job's UUID
            admin_id: Admin's UUID (for access control)

        Returns:
            OrganisationJobResponse if found and accessible

        Raises:
            JobNotFoundError: If job not found or not accessible
        """
        from src.service.admin.organisation_job_service import organisation_job_service

        job = await organisation_job_service.get_job_by_admin(
            session=self._session,
            job_id=job_id,
            admin_id=admin_id,
        )
        if not job:
            raise JobNotFoundError(
                "Job not found or access denied",
                error_code="JOB_NOT_FOUND",
            )
        return OrganisationJobResponse.model_validate(job)

    async def list_jobs(
        self,
        admin_id: UUID,
        status: OrganisationJobStatus | None,
        page: int,
        page_size: int,
    ) -> OrganisationJobListResponse:
        """List jobs for an admin.

        Args:
            admin_id: Admin's UUID
            status: Optional status filter
            page: Page number (1-indexed)
            page_size: Number of items per page

        Returns:
            OrganisationJobListResponse with jobs and pagination info
        """
        from src.service.admin.organisation_job_service import organisation_job_service

        offset = (page - 1) * page_size
        jobs, total = await organisation_job_service.list_jobs_by_admin(
            session=self._session,
            admin_id=admin_id,
            status=status,
            limit=page_size,
            offset=offset,
        )

        return OrganisationJobListResponse(
            jobs=[OrganisationJobResponse.model_validate(j) for j in jobs],
            total=total,
            page=page,
            page_size=page_size,
        )
