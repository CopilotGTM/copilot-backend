"""Deal memory service — build/update and backfill orchestration.

The service does NOT load deal data for the agent — the agent uses tools.
Service responsibility:
- build_or_update: load existing memory, call agent, save result
- backfill_org: find deals without memory, create jobs for them
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm.activities import CRMActivity
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.domain.models.organisation_tables.sync.deal_memory_models import DealMemory
from src.domain.models.organisation_tables.sync.email_models import Email
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.utils.deal_job_utils import get_deal, get_organisation_name

logger = get_logger(__name__)

AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


class DealMemoryError(Exception):
    """Base exception for deal memory errors."""

    def __init__(self, message: str, error_code: str = "memory_error", retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.retryable = retryable


class DealMemoryService:
    """Service for building and updating persistent deal memory documents.

    Orchestrates the deal memory lifecycle:
    1. Loads existing memory (if any) and validates the deal
    2. Calls DealMemoryAgent (which uses tools to gather data)
    3. Saves the generated/updated memory document

    Also handles backfill: finding open deals without memory that have
    data (meetings, emails, or notes) and creating jobs for them.

    Follows fresh session pattern:
    - Phase 1: Load context (fresh session, release before LLM)
    - Phase 2: Call agent (agent manages its own sessions via tools)
    - Phase 3: Save result (fresh session)

    Usage:
        service = DealMemoryService(org_id, publisher)
        content = await service.build_or_update(deal_id)
    """

    def __init__(
        self,
        organisation_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
    ):
        """Initialize the service.

        Args:
            organisation_id: Organisation ID (security boundary)
            publisher: Optional Redis publisher for SSE progress updates
        """
        self.organisation_id = organisation_id
        self.publisher = publisher
        self.last_token_usage = None

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def build_or_update(
        self,
        deal_id: UUID,
        *,
        trigger_type: str | None = None,
        trigger_source_id: UUID | None = None,
    ) -> str:
        """Build or update the deal memory document.

        The agent uses tools to gather meetings, emails, notes, and
        stakeholder data, then synthesizes into a persistent markdown document.

        Follows fresh session pattern:
        - Phase 1: Load existing memory + validate deal (fresh session)
        - Phase 2: Call agent (agent manages its own sessions via tools)
        - Phase 3: Save result (fresh session)

        Args:
            deal_id: The deal ID to build/update memory for
            trigger_type: What triggered this update (e.g. "new_meeting", "new_email")
            trigger_source_id: ID of the triggering entity (e.g. meeting_id)

        Returns:
            The updated markdown content string

        Raises:
            DealMemoryError: If deal not found (retryable=False) or AI fails (retryable=True)
        """
        from src.agents.workflows.deal_memory import DealMemoryAgent

        # Phase 1: Load existing memory + basic deal validation (fresh session)
        self._publish_progress("Loading deal context...", "context", 0.1)

        is_initial = True
        org_name: str | None = None

        async for session in acquire_org_async_session(self.organisation_id):
            deal = await get_deal(session, self.organisation_id, deal_id)
            if not deal:
                raise DealMemoryError(
                    f"Deal {deal_id} not found",
                    error_code="deal_not_found",
                    retryable=False,
                )

            memory_row = (
                (
                    await session.execute(
                        select(DealMemory).where(
                            col(DealMemory.deal_id) == deal_id,
                            col(DealMemory.organisation_id) == self.organisation_id,
                        )
                    )
                )
                .scalars()
                .first()
            )

            if memory_row:
                is_initial = False
        # Org session released

        # Org name lives in public schema — use a master session
        async for master_session in acquire_master_async_session():
            org_name = await get_organisation_name(master_session, self.organisation_id)

        # Phase 2: Call agent (agent uses tools to gather data, no session held here)
        self._publish_progress("AI is building deal memory...", "generation", 0.3)

        agent = DealMemoryAgent(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            publisher=self.publisher,
            org_name=org_name,
        )

        try:
            content = await agent.generate(
                is_initial_build=is_initial,
                trigger_type=trigger_type,
                trigger_source_id=(str(trigger_source_id) if trigger_source_id else None),
            )
            self.last_token_usage = agent.last_token_usage
        except Exception as exc:
            raise DealMemoryError(
                f"AI generation failed: {exc}",
                error_code="ai_generation_failed",
                retryable=True,
            ) from exc

        # Phase 3: Save result (fresh session)
        self._publish_progress("Saving deal memory...", "saving", 0.9)

        token_count = len(content.split()) * 2  # rough estimate

        async for session in acquire_org_async_session(self.organisation_id):
            memory_row = (
                (
                    await session.execute(
                        select(DealMemory).where(
                            col(DealMemory.deal_id) == deal_id,
                            col(DealMemory.organisation_id) == self.organisation_id,
                        )
                    )
                )
                .scalars()
                .first()
            )

            if memory_row:
                memory_row.content = content
                memory_row.version = memory_row.version + 1
                memory_row.token_count = token_count
            else:
                session.add(
                    DealMemory(
                        organisation_id=self.organisation_id,
                        deal_id=deal_id,
                        content=content,
                        version=1,
                        token_count=token_count,
                    )
                )
            await session.commit()

        logger.info(
            "Deal memory %s for deal %s (~%d tokens)",
            "created" if is_initial else "updated",
            deal_id,
            token_count,
        )
        return content

    async def backfill_org(self) -> dict[str, Any]:
        """Find open deals without memory that have data, and create jobs for them.

        Three-phase bulk approach:
        1. Query open deals that don't already have a memory row (limit 50)
        2. Filter to deals that have at least one meeting, email, or note (UNION query)
        3. Create DealJobs for eligible deals in a single commit

        Called by the Tier 2 backfill task (build_deal_memory_org).

        Returns:
            Dict with 'jobs_to_dispatch' (list of (org_id, job_id, deal_id) tuples),
            'dispatched' count, and optionally 'skipped' count.
        """
        org_id = self.organisation_id

        # Phase 1: Find open deals without memory
        async for session in acquire_org_async_session(org_id):
            existing_memory_ids = select(col(DealMemory.deal_id)).where(
                col(DealMemory.organisation_id) == org_id
            )
            deals_result = await session.execute(
                select(col(CRMDeal.deal_id))
                .where(
                    col(CRMDeal.organisation_id) == org_id,
                    col(CRMDeal.is_deleted).is_(False),
                    col(CRMDeal.deal_status) == CRMDealStatus.OPEN,
                    col(CRMDeal.deal_id).notin_(existing_memory_ids),
                )
                .limit(50)
            )
            candidate_ids = [row[0] for row in deals_result.all()]
        # Session released

        if not candidate_ids:
            return {"jobs_to_dispatch": [], "dispatched": 0, "skipped": 0}

        # Phase 2: Filter to only deals that have data (single bulk query)
        eligible_ids = await self._deals_with_data(candidate_ids)

        if not eligible_ids:
            return {"jobs_to_dispatch": [], "dispatched": 0, "skipped": len(candidate_ids)}

        # Phase 3: Create jobs for eligible deals (single session, one commit)
        jobs: list[DealJobs] = []

        async for session in acquire_org_async_session(org_id):
            for deal_id in eligible_ids:
                job = DealJobs(
                    organisation_id=org_id,
                    user_id=AUTOMATED_PIPELINE_USER_ID,
                    job_type=DealJobType.DEAL_MEMORY_BUILD,
                    deal_id=deal_id,
                    status=JobStatus.PENDING,
                    job_metadata={
                        "source": "automated_pipeline",
                        "is_initial_build": True,
                    },
                )
                session.add(job)
                jobs.append(job)
            await session.commit()
            # Session released

        return {
            "jobs_to_dispatch": [(str(org_id), str(j.job_id), str(j.deal_id)) for j in jobs],
            "dispatched": len(jobs),
            "skipped": len(candidate_ids) - len(eligible_ids),
        }

    async def _deals_with_data(self, deal_ids: list[UUID]) -> list[UUID]:
        """Return the subset of deal_ids that have at least one data source.

        Uses a single UNION query across CalendarEventDealAssociation, Email,
        and CRMActivity (notes) to find deals with data in one round-trip.

        Args:
            deal_ids: Candidate deal IDs to check

        Returns:
            List of deal IDs that have meetings, emails, or notes
        """
        async for session in acquire_org_async_session(self.organisation_id):
            # UNION of deal_ids across all three data sources, filtered to candidates
            deals_with_meetings = select(col(CalendarEventDealAssociation.deal_id)).where(
                col(CalendarEventDealAssociation.deal_id).in_(deal_ids)
            )

            deals_with_emails = select(col(Email.deal_id)).where(col(Email.deal_id).in_(deal_ids))

            deals_with_notes = select(col(CRMActivity.deal_id)).where(
                col(CRMActivity.deal_id).in_(deal_ids),
                col(CRMActivity.activity_type) == "NOTE",
            )

            combined = deals_with_meetings.union(deals_with_emails, deals_with_notes)

            result = await session.execute(combined)
            return [row[0] for row in result.all()]

        return []
