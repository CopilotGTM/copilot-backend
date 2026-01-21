"""Service for generating deal summaries.

This service handles the business logic for deal summary generation:
- Loading minimal deal context (counts, date ranges)
- Passing context to the DealSummaryAgent which uses tools
- Saving results to CRMDeal

The agent uses tools to gather stakeholder, meeting, and email data as needed.
The Celery task delegates to this service for the actual work.

Follows fresh session pattern:
- Phase 1: Load context (fresh session, release before LLM)
- Phase 2: Call agent (agent manages its own session)
- Phase 3: Save result (fresh session)
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.utils.deal_job_utils import (
    count_deal_emails,
    count_deal_meetings,
    count_deal_stakeholders,
    get_deal,
    get_deal_meeting_date_range,
)

logger = get_logger(__name__)

# Current schema version for generated content payloads
SCHEMA_VERSION = "1.0"


class DealSummaryError(Exception):
    """Base exception for deal summary errors."""

    def __init__(self, message: str, error_code: str = "summary_error", retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.retryable = retryable


class DealNotFoundError(DealSummaryError):
    """Raised when deal is not found."""

    def __init__(self, deal_id: UUID):
        super().__init__(
            f"Deal {deal_id} not found",
            error_code="deal_not_found",
            retryable=False,
        )


class NoMeetingsError(DealSummaryError):
    """Raised when no meetings are found for the deal."""

    def __init__(self, deal_id: UUID):
        super().__init__(
            f"No meetings found for deal {deal_id}. Schedule meetings before generating a summary.",
            error_code="no_meetings",
            retryable=False,
        )


def wrap_generated_content(content: dict) -> dict:
    """Wrap generated content with metadata envelope.

    Args:
        content: The generated content dictionary

    Returns:
        Dictionary with schema_version, generated_at, and data fields
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "data": content,
    }


class DealSummaryService:
    """Service for generating comprehensive deal summaries.

    This service orchestrates the deal summary generation process:
    1. Loads deal and stakeholder context
    2. Fetches all meetings and their transcripts
    3. Passes context to the DealSummaryAgent (which uses tools)
    4. Saves results to the CRMDeal

    Follows fresh session pattern:
    - Phase 1: Load context (fresh session, release before LLM)
    - Phase 2: Call agent (agent manages its own session)
    - Phase 3: Save result (fresh session)

    Usage:
        service = DealSummaryService(org_id, publisher)
        result = await service.generate_summary(deal_id)
    """

    def __init__(
        self,
        organisation_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
    ):
        """Initialize the service.

        Args:
            organisation_id: Organisation ID
            publisher: Optional Redis publisher for progress updates
        """
        self.organisation_id = organisation_id
        self.publisher = publisher

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def generate_summary(self, deal_id: UUID) -> dict:
        """Generate a comprehensive summary for a deal.

        The agent uses tools to gather data about stakeholders, meetings,
        and emails, then synthesizes into a comprehensive summary.

        Follows fresh session pattern:
        - Phase 1: Load context (fresh session, release before LLM)
        - Phase 2: Call agent (agent manages its own session)
        - Phase 3: Save result (fresh session)

        Args:
            deal_id: The deal ID to generate summary for

        Returns:
            Wrapped result dictionary with schema_version, generated_at, and data

        Raises:
            DealNotFoundError: If deal doesn't exist
            NoMeetingsError: If no meetings are associated with the deal
        """
        # Import here to avoid circular imports
        from src.agents.workflows.deal_summary import DealSummaryAgent, DealSummaryInput

        # Phase 1: Load context (fresh session, release before agent call)
        self._publish_progress("Loading deal context...", "context", 0.1)

        # Variables to hold context after session is released
        deal_name: str = ""
        company_name: str | None = None
        deal_stage: str | None = None
        deal_amount: float | None = None
        currency: str | None = None
        close_date: str | None = None
        deal_status: str = "open"
        meeting_count: int = 0
        stakeholder_count: int = 0
        email_count: int = 0
        first_date: str | None = None
        last_date: str | None = None

        async for session in acquire_org_async_session(self.organisation_id):
            # 1. Load deal
            deal = await get_deal(session, self.organisation_id, deal_id)
            if not deal:
                raise DealNotFoundError(deal_id)

            self._publish_progress("Checking deal data...", "checking", 0.15)

            # 2. Get counts for agent planning
            meeting_count = await count_deal_meetings(session, self.organisation_id, deal_id)
            if meeting_count == 0:
                raise NoMeetingsError(deal_id)

            stakeholder_count = await count_deal_stakeholders(
                session, self.organisation_id, deal_id
            )
            email_count = await count_deal_emails(session, self.organisation_id, deal_id)

            # 3. Get meeting date range
            first_date, last_date = await get_deal_meeting_date_range(
                session, self.organisation_id, deal_id
            )

            # Extract values before session is released
            deal_name = deal.name or "Unnamed Deal"
            company_name = deal.company_name
            deal_stage = deal.stage_label or deal.stage_name
            deal_amount = float(deal.amount) if deal.amount else None
            currency = deal.currency
            close_date = str(deal.close_date) if deal.close_date else None
            deal_status = str(deal.deal_status) if deal.deal_status else "open"
        # Session released

        self._publish_progress(
            f"Found {meeting_count} meetings, {stakeholder_count} stakeholders. Starting AI analysis...",
            "preparing",
            0.2,
        )

        # 4. Build minimal input for the agent
        input_data = DealSummaryInput(
            deal_id=str(deal_id),
            deal_name=deal_name,
            company_name=company_name,
            deal_stage=deal_stage,
            deal_amount=deal_amount,
            currency=currency,
            close_date=close_date,
            deal_status=deal_status,
            meeting_count=meeting_count,
            stakeholder_count=stakeholder_count,
            email_count=email_count,
            first_meeting_date=first_date,
            last_meeting_date=last_date,
        )

        logger.info(
            "Starting deal summary generation for deal %s: %d meetings, %d stakeholders, %d emails",
            deal_id,
            meeting_count,
            stakeholder_count,
            email_count,
        )

        # Phase 2: Call agent (tools acquire sessions on-demand)
        # Agent doesn't hold sessions - tools acquire on-demand via context.acquire_session()
        # SECURITY: Both organisation_id and deal_id are baked into agent context (immutable)
        agent = DealSummaryAgent(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            publisher=self.publisher,
        )
        summary_data = await agent.generate(input_data)

        self._publish_progress("Saving results...", "saving", 0.95)

        # 6. Wrap result with metadata
        summary_dict = summary_data.model_dump()
        wrapped_result = wrap_generated_content(summary_dict)

        # Phase 3: Save result (fresh session)
        async for session in acquire_org_async_session(self.organisation_id):
            crm_deal = await session.get(CRMDeal, deal_id)
            if crm_deal:
                crm_deal.deal_summary_data = wrapped_result
                await session.commit()
                logger.info("Saved deal summary to CRMDeal %s", deal_id)

        return wrapped_result
