"""Service for generating deal summaries.

This service handles the business logic for deal summary generation:
- Validating deal existence and fetching org name
- Passing context to the DealSummaryAgent which uses tools to gather ALL data
- Saving results to CRMDeal

The agent uses tools to gather stakeholder, meeting, and email data as needed.
The Celery task delegates to this service for the actual work.

Follows fresh session pattern:
- Phase 1: Validate deal exists, get org_name (fresh session, release before LLM)
- Phase 2: Call agent (agent manages its own session via tools)
- Phase 3: Save result (fresh session)
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.utils.deal_job_utils import (
    get_deal,
    get_organisation_agent_context,
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
    1. Validates deal exists and fetches org name
    2. Creates DealSummaryAgent with org_id + deal_id (agent gathers all data via tools)
    3. Saves results to the CRMDeal

    Follows fresh session pattern:
    - Phase 1: Validate deal, get org_name (fresh session, release before LLM)
    - Phase 2: Call agent (agent manages its own sessions via tools)
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
        self.last_token_usage = None

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    async def generate_summary(self, deal_id: UUID) -> dict:
        """Generate a comprehensive summary for a deal.

        The agent uses tools to gather data about stakeholders, meetings,
        and emails, then synthesizes into a comprehensive summary.

        Follows fresh session pattern:
        - Phase 1: Validate deal, get org_name (fresh session, release before LLM)
        - Phase 2: Call agent (agent manages its own sessions via tools)
        - Phase 3: Save result (fresh session)

        Args:
            deal_id: The deal ID to generate summary for

        Returns:
            Wrapped result dictionary with schema_version, generated_at, and data

        Raises:
            DealNotFoundError: If deal doesn't exist
        """
        from src.agents.workflows.deal_summary import DealSummaryAgent

        # Phase 1: Validate deal exists, get org_name (release before LLM)
        self._publish_progress("Loading deal context...", "context", 0.1)

        org_name: str | None = None
        organisation_profile: str | None = None

        async for session in acquire_org_async_session(self.organisation_id):
            deal = await get_deal(session, self.organisation_id, deal_id)
            if not deal:
                raise DealNotFoundError(deal_id)
        # Session released

        # Org name lives in public schema — use a master session
        async for master_session in acquire_master_async_session():
            org_name, organisation_profile = await get_organisation_agent_context(
                master_session, self.organisation_id
            )

        logger.info(
            "Starting deal summary generation for deal %s",
            deal_id,
            extra={
                "event": "deal_summary_started",
                "organisation_id": str(self.organisation_id),
                "deal_id": str(deal_id),
            },
        )

        self._publish_progress("Starting AI analysis...", "preparing", 0.2)

        # Phase 2: Call agent (tools acquire sessions on-demand)
        agent = DealSummaryAgent(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            publisher=self.publisher,
            org_name=org_name,
            organisation_profile=organisation_profile,
        )
        try:
            summary_data = await agent.generate()
            self.last_token_usage = agent.last_token_usage
        except DealSummaryError:
            raise
        except Exception as exc:
            raise DealSummaryError(
                f"AI summary generation failed: {exc}",
                error_code="ai_generation_failed",
                retryable=True,
            ) from exc

        self._publish_progress("Saving results...", "saving", 0.95)

        if isinstance(summary_data, dict):
            summary_dict = summary_data
        elif hasattr(summary_data, "model_dump"):
            summary_dict = summary_data.model_dump()
        else:
            raise DealSummaryError(
                "AI summary generation failed: Deal summary agent returned unsupported payload "
                f"type: {type(summary_data).__name__}.",
                error_code="ai_generation_failed",
                retryable=True,
            )
        wrapped_result = wrap_generated_content(summary_dict)

        # Phase 3: Save result (fresh session)
        async for session in acquire_org_async_session(self.organisation_id):
            crm_deal = await session.get(CRMDeal, deal_id)
            if crm_deal:
                crm_deal.deal_summary_data = wrapped_result
                await session.commit()
                logger.info(
                    "Saved deal summary to CRMDeal %s",
                    deal_id,
                    extra={
                        "event": "deal_summary_saved",
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(deal_id),
                    },
                )

        return wrapped_result
