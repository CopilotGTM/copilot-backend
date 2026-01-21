"""Risk/Blocker extraction service for processing deal content.

This service handles the business logic for risk/blocker extraction:
- Loading minimal deal context (counts, date ranges)
- Passing context to the RiskBlockerExtractionAgent which uses tools
- Creating AI-suggested risk/blocker records

The agent uses tools to gather meeting transcripts, emails, and existing
risks/blockers as needed.

Follows fresh session pattern:
- Phase 1: Load context (fresh session, release before LLM)
- Phase 2: Call agent (agent manages its own sessions via tools)
- Phase 3: Save result (fresh session)

The Celery task delegates to this service for the actual work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from src.agents.workflows.risk_blocker_extraction import (
    ExtractedRiskBlocker,
    RiskBlockerExtractionAgent,
    RiskBlockerExtractionInput,
    RiskBlockerExtractionResult,
)
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerSource,
    RiskBlockerStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.utils.deal_job_utils import (
    count_deal_emails,
    count_deal_meetings,
    count_deal_risks_blockers,
    get_deal,
    get_deal_meeting_date_range,
)

logger = get_logger(__name__)


class RiskBlockerExtractionError(Exception):
    """Base exception for extraction errors."""

    def __init__(self, message: str, error_code: str = "extraction_error", retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.retryable = retryable


class DealNotFoundError(RiskBlockerExtractionError):
    """Raised when deal is not found."""

    def __init__(self, deal_id: UUID):
        super().__init__(
            f"Deal {deal_id} not found",
            error_code="deal_not_found",
            retryable=False,
        )


class NoContentError(RiskBlockerExtractionError):
    """Raised when no content is available for analysis."""

    def __init__(self, deal_id: UUID):
        super().__init__(
            f"No meetings or emails found for deal {deal_id}. Add communications before extracting risks.",
            error_code="no_content",
            retryable=False,
        )


class RiskBlockerExtractionService:
    """Service for extracting risks and blockers from deal content.

    This service orchestrates the extraction process:
    1. Loads minimal deal context (counts, date ranges)
    2. Passes context to the RiskBlockerExtractionAgent (which uses tools)
    3. Creates AI-suggested risk/blocker records from extracted items

    The agent uses tools to gather:
    - Existing risks/blockers for deduplication
    - Meeting transcripts for analysis
    - Email content for analysis

    Follows fresh session pattern:
    - Phase 1: Load context (fresh session, release before LLM)
    - Phase 2: Call agent (agent manages its own sessions via tools)
    - Phase 3: Save result (fresh session)

    Usage:
        service = RiskBlockerExtractionService(org_id, publisher)
        result = await service.run_extraction(deal_id)
    """

    def __init__(
        self,
        organisation_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
    ):
        """Initialize the service.

        SECURITY BOUNDARY: organisation_id is immutable - all queries use this value.

        Args:
            organisation_id: UUID of the organisation (IMMUTABLE - security boundary)
            publisher: Optional Redis publisher for progress updates
        """
        self.organisation_id = organisation_id
        self.publisher = publisher

    def _publish_progress(self, message: str, step: str, progress: float) -> None:
        """Publish progress update if publisher is available."""
        if self.publisher:
            self.publisher.publish_progress(message, step, progress)

    def create_risk_blocker_from_extracted(
        self,
        session: Any,
        deal_id: UUID,
        extracted: ExtractedRiskBlocker,
        source_type: RiskBlockerSource,
        source_record_id: str | None = None,
    ) -> DealRiskBlocker:
        """Create a risk/blocker record from an extracted item.

        Args:
            session: Database session
            deal_id: UUID of the deal
            extracted: The extracted risk/blocker
            source_type: Source type (MEETING, EMAIL, etc.)
            source_record_id: Optional source record ID

        Returns:
            Created DealRiskBlocker
        """
        item = DealRiskBlocker(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            item_type=extracted.item_type,
            category=extracted.category,
            severity=extracted.severity,
            status=RiskBlockerStatus.AI_SUGGESTED,
            title=extracted.title,
            description=extracted.description,
            quote=extracted.quote,
            source_type=source_type,
            source_record_id=source_record_id,
            source_context=extracted.source_context,
            is_ai_generated=True,
            ai_confidence=extracted.confidence,
            ai_reasoning=extracted.reasoning,
            owner_name=extracted.suggested_owner,
            risk_blocker_data={
                "audit_trail": [
                    {
                        "action": "ai_extracted",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "actor_type": "system",
                        "actor_id": "risk_blocker_extraction_agent",
                        "details": {
                            "confidence": extracted.confidence,
                        },
                    }
                ],
                "unique_modifiers": [],
            },
        )

        session.add(item)
        return item

    async def run_extraction(
        self,
        deal_id: UUID,
        force: bool = False,
    ) -> RiskBlockerExtractionResult:
        """Run the full extraction process for a deal.

        The agent uses tools to gather data about meetings, emails, and
        existing risks/blockers, then identifies new items.

        Follows fresh session pattern:
        - Phase 1: Load context (fresh session, release before LLM)
        - Phase 2: Call agent (agent manages its own sessions via tools)
        - Phase 3: Save result (fresh session)

        Args:
            deal_id: UUID of the deal to extract from
            force: Reserved for future use (re-process all sources)

        Returns:
            RiskBlockerExtractionResult with extraction results

        Raises:
            DealNotFoundError: If deal doesn't exist
            NoContentError: If no meetings or emails are available
        """
        _ = force  # Reserved for future use

        # Phase 1: Load context (fresh session, release before LLM)
        self._publish_progress("Loading deal context...", "context", 0.1)

        # Variables to hold context after session is released
        deal_name: str = ""
        company_name: str | None = None
        deal_stage: str | None = None
        meeting_count: int = 0
        email_count: int = 0
        risks_count: int = 0
        blockers_count: int = 0
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
            email_count = await count_deal_emails(session, self.organisation_id, deal_id)

            if meeting_count == 0 and email_count == 0:
                raise NoContentError(deal_id)

            # 3. Get existing risks/blockers count
            risks_blockers = await count_deal_risks_blockers(session, self.organisation_id, deal_id)
            risks_count = risks_blockers["risks"]
            blockers_count = risks_blockers["blockers"]

            # 4. Get meeting date range
            first_date, last_date = await get_deal_meeting_date_range(
                session, self.organisation_id, deal_id
            )

            # Extract values before session is released
            deal_name = deal.name or "Unnamed Deal"
            company_name = deal.company_name
            deal_stage = deal.stage_label or deal.stage_name
        # Session released

        self._publish_progress(
            f"Found {meeting_count} meetings, {email_count} emails. Starting AI analysis...",
            "preparing",
            0.2,
        )

        # 5. Build minimal input for the agent
        input_data = RiskBlockerExtractionInput(
            deal_id=str(deal_id),
            deal_name=deal_name,
            company_name=company_name,
            deal_stage=deal_stage,
            meeting_count=meeting_count,
            email_count=email_count,
            existing_risks_count=risks_count,
            existing_blockers_count=blockers_count,
            first_meeting_date=first_date,
            last_meeting_date=last_date,
        )

        logger.info(
            "Starting risk/blocker extraction for deal %s: %d meetings, %d emails, %d existing",
            deal_id,
            meeting_count,
            email_count,
            risks_count + blockers_count,
        )

        # Phase 2: Call agent (tools acquire sessions on-demand)
        # Agent doesn't hold sessions - tools acquire on-demand via context.acquire_session()
        # SECURITY: Both organisation_id and deal_id are baked into agent context (immutable)
        agent = RiskBlockerExtractionAgent(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            publisher=self.publisher,
        )
        result = await agent.extract(input_data)

        # Phase 3: Save result (fresh session)
        self._publish_progress("Creating risk/blocker records...", "saving", 0.9)

        created_count = 0

        async for session in acquire_org_async_session(self.organisation_id):
            # Create records for non-duplicate items
            for extracted_item in result.items:
                if not extracted_item.is_duplicate:
                    # Use source_type from agent output (agent specifies exact platform)
                    source_type = extracted_item.source_type

                    self.create_risk_blocker_from_extracted(
                        session=session,
                        deal_id=deal_id,
                        extracted=extracted_item,
                        source_type=source_type,
                    )
                    created_count += 1

            # Commit all new records
            await session.commit()

        self._publish_progress("Extraction complete!", "complete", 1.0)

        logger.info(
            "Extraction complete for deal %s: %d items extracted, %d new, %d duplicates",
            deal_id,
            len(result.items),
            created_count,
            result.duplicate_items_count,
        )

        return result

    async def process_deal(
        self,
        deal_id: UUID,
        force: bool = False,
    ) -> dict[str, Any]:
        """Process a deal for risk/blocker extraction (job entry point).

        Args:
            deal_id: UUID of the deal
            force: Whether to force re-processing

        Returns:
            Dictionary with processing results
        """
        try:
            result = await self.run_extraction(deal_id, force=force)

            return {
                "success": True,
                "deal_id": str(deal_id),
                "total_items": len(result.items),
                "risks_count": result.total_risks,
                "blockers_count": result.total_blockers,
                "new_items_count": result.new_items_count,
                "duplicate_items_count": result.duplicate_items_count,
                "sources_analyzed": result.sources_analyzed,
                "analysis_summary": result.analysis_summary,
            }

        except RiskBlockerExtractionError as e:
            logger.warning("Extraction failed for deal %s: %s", deal_id, e.message)
            return {
                "success": False,
                "deal_id": str(deal_id),
                "error": e.message,
                "error_code": e.error_code,
                "retryable": e.retryable,
            }

        except Exception as e:
            logger.exception("Unexpected error during extraction for deal %s", deal_id)
            return {
                "success": False,
                "deal_id": str(deal_id),
                "error": str(e),
                "error_code": "unexpected_error",
                "retryable": True,
            }
