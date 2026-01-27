"""Service for managing deal notes (CRM-synced and user-created)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from celery import chain
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger

logger = get_logger("deal_notes_service")


@dataclass
class ParticipantInfo:
    """Simple container for participant data returned by the service."""

    email: str
    name: str | None


class DealNotesServiceError(Exception):
    """Custom exception for deal notes service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealNotesService:
    """Service for managing deal notes (CRM-synced and user-created).

    Notes are stored in the crm_activities table with activity_type = NOTE.
    User-created notes have integration_id = NULL to distinguish from CRM-synced notes.
    """

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("DealNotesService close exception", exc_info=True)

    @property
    def session(self) -> AsyncSession:
        return self.org_session

    async def get_deal_notes(
        self,
        deal_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        sort_order: str = "desc",
    ) -> tuple[list[CRMActivity], str | None, int, list[ParticipantInfo]]:
        """Retrieve notes for a specific deal with cursor-based pagination.

        Args:
            deal_id: Internal deal UUID
            limit: Maximum number of notes to return
            cursor: Pagination cursor from previous response
            sort_order: Sort order (desc = newest first, asc = oldest first)

        Returns:
            Tuple of (notes list, next cursor, total count, participants)

        Raises:
            DealNotesServiceError: If deal not found or access denied
        """
        # Verify deal exists and belongs to this organization
        deal = await self._verify_deal_access(deal_id)

        c = cast(Any, CRMActivity).__table__.c

        # Use a stable, null-safe timestamp for sorting/pagination.
        # CRM notes should have source_created_at, but user-created notes and
        # some provider payloads may not.
        sort_ts = func.coalesce(c.source_created_at, c.created_at)

        # Base query for notes linked to this deal
        base_where = and_(
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
            c.activity_type == CRMActivityType.NOTE.value,
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )

        # Get total count
        count_stmt = select(func.count()).select_from(CRMActivity).where(base_where)
        count_result = await self.org_session.execute(count_stmt)
        total_count = count_result.scalar() or 0

        # Build notes query
        stmt = select(CRMActivity).where(base_where)

        # Handle cursor-based pagination
        if cursor:
            try:
                ts, activity_id = decode_cursor(cursor)
            except Exception as exc:
                raise DealNotesServiceError("Invalid cursor", status_code=400) from exc

            if sort_order == "desc":
                stmt = stmt.where(
                    or_(
                        sort_ts < ts,
                        and_(sort_ts == ts, c.activity_id < activity_id),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        sort_ts > ts,
                        and_(sort_ts == ts, c.activity_id > activity_id),
                    )
                )

        # Apply sorting
        if sort_order == "desc":
            stmt = stmt.order_by(sort_ts.desc(), c.activity_id.desc())
        else:
            stmt = stmt.order_by(sort_ts.asc(), c.activity_id.asc())

        # Fetch one extra to determine if there are more results
        stmt = stmt.limit(limit + 1)

        result = await self.org_session.execute(stmt)
        rows: list[CRMActivity] = list(result.scalars().all())

        # Determine if there are more results
        has_more = len(rows) > limit
        items = rows[:limit]

        # Build next cursor if there are more results
        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(last.source_created_at or last.created_at, last.activity_id)

        # Fetch participants (deal owner + stakeholders) for @mentions
        participants = await self._get_deal_participants(deal_id, deal)

        return items, next_cursor, total_count, participants

    async def _get_deal_participants(self, deal_id: UUID, deal: CRMDeal) -> list[ParticipantInfo]:
        """Fetch all participants for a deal (owner + stakeholders).

        Simple approach: separate query for stakeholders, add deal owner from deal object.
        No complex joins needed.
        """
        participants: list[ParticipantInfo] = []
        seen_emails: set[str] = set()

        # Add deal owner if available
        if deal.owner_email:
            owner_email = deal.owner_email.lower()
            seen_emails.add(owner_email)
            participants.append(
                ParticipantInfo(
                    email=deal.owner_email,
                    name=deal.owner_name,
                )
            )

        # Fetch stakeholders for this deal
        stakeholders_stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await self.org_session.execute(stakeholders_stmt)
        stakeholders = list(result.scalars().all())

        for s in stakeholders:
            if not s.email:
                continue
            email_lower = s.email.lower()
            # Avoid duplicates (e.g., owner is also a stakeholder)
            if email_lower in seen_emails:
                continue
            seen_emails.add(email_lower)
            participants.append(
                ParticipantInfo(
                    email=s.email,
                    name=s.name,
                )
            )

        return participants

    async def create_user_note(
        self,
        deal_id: UUID,
        content: str,
        subject: str | None,
        user_id: UUID,
        user_name: str | None,
        user_email: str | None,
    ) -> CRMActivity:
        """Create a new user-generated note for a deal.

        User-created notes are distinguished from CRM-synced notes by:
        - integration_id = NULL
        - properties contains {"is_user_created": True, "created_via": "api"}

        Args:
            deal_id: Internal deal UUID
            content: Note content/body
            subject: Note subject/title (optional)
            user_id: ID of user creating the note
            user_name: Name of user creating the note
            user_email: Email of user creating the note

        Returns:
            Created CRMActivity note record

        Raises:
            DealNotesServiceError: If deal not found or access denied
        """
        # Verify deal exists and get deal details
        deal = await self._verify_deal_access(deal_id)

        now = datetime.now(UTC)
        note_id = uuid4()

        note = CRMActivity(
            activity_id=note_id,
            organisation_id=self.organisation_id,
            integration_id=None,  # NULL indicates user-created
            external_id=str(note_id),  # Use our UUID as external_id
            activity_type=CRMActivityType.NOTE.value,
            subject=subject,
            body=content,
            deal_id=deal_id,
            deal_external_id=deal.external_id,
            owner_external_id=str(user_id),  # Store user_id as string
            owner_name=user_name,
            owner_email=user_email,
            source_created_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={
                "is_user_created": True,
                "created_via": "api",
            },
        )

        self.org_session.add(note)
        await self.org_session.flush()
        await self.org_session.refresh(note)

        logger.info(
            "Created user note %s for deal %s by user %s",
            note.activity_id,
            deal_id,
            user_id,
        )

        return note

    async def update_user_note(
        self,
        deal_id: UUID,
        note_id: UUID,
        payload: dict[str, Any],
    ) -> CRMActivity:
        """Update a user-created note for a deal."""
        if not payload:
            raise DealNotesServiceError("No fields to update", status_code=400)

        note = await self._load_user_note(deal_id, note_id)

        updated = False
        if "content" in payload:
            if payload["content"] is None:
                raise DealNotesServiceError("Content cannot be null", status_code=400)
            note.body = payload["content"]
            updated = True
        if "subject" in payload:
            note.subject = payload["subject"]
            updated = True

        if not updated:
            raise DealNotesServiceError("No fields to update", status_code=400)

        now = datetime.now(UTC)
        note.source_modified_at = now
        note.synced_at = now
        note.last_seen_at = now
        properties = note.properties or {}
        properties["updated_via"] = "api"
        note.properties = properties

        await self.org_session.flush()
        await self.org_session.refresh(note)

        return note

    async def delete_user_note(self, deal_id: UUID, note_id: UUID) -> CRMActivity:
        """Soft delete a user-created note for a deal."""
        note = await self._load_user_note(deal_id, note_id)

        now = datetime.now(UTC)
        note.is_deleted = True
        note.source_modified_at = now
        note.synced_at = now
        note.last_seen_at = now
        properties = note.properties or {}
        properties["deleted_via"] = "api"
        note.properties = properties

        await self.org_session.flush()
        await self.org_session.refresh(note)

        return note

    async def trigger_deal_insight_pipeline(
        self,
        deal_id: UUID,
        user_id: UUID,
        trigger_source: str = "note_creation",
    ) -> UUID | None:
        """Trigger the deal insight pipeline after note creation.

        Creates a job for deal summary (with deduplication) and triggers:
        risk_blocker → stakeholder → deal_summary chain.

        Args:
            deal_id: The deal UUID to process
            user_id: User ID for job attribution
            trigger_source: Source of the trigger for metadata

        Returns:
            The job ID if created/found, None if trigger failed
        """
        try:
            org_id_str = str(self.organisation_id)
            deal_id_str = str(deal_id)

            # Check for existing active deal summary job
            stmt = select(DealJobs).where(
                col(DealJobs.organisation_id) == self.organisation_id,
                col(DealJobs.deal_id) == deal_id,
                col(DealJobs.event_id).is_(None),
                col(DealJobs.job_type) == DealJobType.DEAL_SUMMARY,
                col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
            )
            result = await self.org_session.execute(stmt)
            existing_job = result.scalars().first()

            if existing_job:
                deal_summary_job_id = existing_job.job_id
            else:
                # Create new job for deal summary
                job = DealJobs(
                    organisation_id=self.organisation_id,
                    user_id=user_id,
                    job_type=DealJobType.DEAL_SUMMARY,
                    deal_id=deal_id,
                    event_id=None,
                    status=JobStatus.PENDING,
                    job_metadata={
                        "source": trigger_source,
                        "trigger": "user_created_note",
                    },
                )
                self.org_session.add(job)
                await self.org_session.flush()
                await self.org_session.refresh(job)
                deal_summary_job_id = job.job_id
                await self.org_session.commit()

            # Build chain: risk_blocker → stakeholder → deal_summary (with job)
            # Use the configured Celery client for proper broker connection
            from src.workers.deal_worker.client import get_deal_celery_client

            celery_app = get_deal_celery_client()
            queue = "deal_jobs"

            risk_blocker_sig = celery_app.signature(
                "risk_blocker.extract_deal_risks_blockers_task",
                args=[org_id_str, deal_id_str],
                queue=queue,
                immutable=True,
            )
            stakeholder_sig = celery_app.signature(
                "stakeholder.analyze_deal_stakeholders_task",
                args=[org_id_str, deal_id_str],
                queue=queue,
                immutable=True,
            )
            deal_summary_sig = celery_app.signature(
                "deal_summary_task.process",
                args=[org_id_str, str(deal_summary_job_id)],
                queue=queue,
                immutable=True,
            )
            task_chain = chain(risk_blocker_sig, stakeholder_sig, deal_summary_sig)
            task_chain.apply_async()

            logger.info(
                "Triggered deal insight pipeline after %s: deal=%s job=%s",
                trigger_source,
                deal_id,
                deal_summary_job_id,
            )
            return deal_summary_job_id

        except Exception as exc:
            logger.warning(
                "Failed to trigger deal insight pipeline: deal=%s error=%s",
                deal_id,
                str(exc),
            )
            return None

    async def _verify_deal_access(self, deal_id: UUID) -> CRMDeal:
        """Verify deal exists and belongs to this organization.

        Args:
            deal_id: Internal deal UUID

        Returns:
            CRMDeal record if found

        Raises:
            DealNotesServiceError: If deal not found or access denied
        """
        c = cast(Any, CRMDeal).__table__.c
        stmt = select(CRMDeal).where(
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
        )
        result = await self.org_session.execute(stmt)
        deal = result.scalar_one_or_none()

        if not deal:
            raise DealNotesServiceError(
                f"Deal {deal_id} not found or access denied",
                status_code=404,
            )

        return deal

    async def _load_user_note(self, deal_id: UUID, note_id: UUID) -> CRMActivity:
        note = await self.org_session.get(CRMActivity, note_id)
        if not note or note.organisation_id != self.organisation_id:
            raise DealNotesServiceError("Note not found", status_code=404)
        if note.deal_id != deal_id:
            raise DealNotesServiceError("Note not found", status_code=404)
        if note.is_deleted:
            raise DealNotesServiceError("Note not found", status_code=404)
        if note.activity_type != CRMActivityType.NOTE.value:
            raise DealNotesServiceError("Note not found", status_code=404)
        if note.integration_id is not None:
            raise DealNotesServiceError("External CRM notes are read-only", status_code=409)
        return note

    def determine_note_source(self, note: CRMActivity) -> str:
        """Determine the source of a note (user, hubspot, salesforce).

        Args:
            note: CRMActivity note record

        Returns:
            Source identifier string
        """
        # User-created notes have no integration_id
        if note.integration_id is None:
            return "user"

        # Check properties for provider hints
        properties: dict[str, Any] = note.properties or {}
        provider_raw = properties.get("provider")
        if isinstance(provider_raw, str):
            provider = provider_raw.strip().lower()
            if provider:
                return provider

        # Use external_id format as heuristic
        # Salesforce IDs typically start with alphanumeric and are 15 or 18 chars
        if note.external_id:
            ext_id = note.external_id
            # Salesforce IDs are 15 or 18 characters, alphanumeric
            if len(ext_id) in (15, 18) and ext_id.isalnum():
                return "salesforce"

        # Default to hubspot for CRM-synced notes
        return "hubspot"
