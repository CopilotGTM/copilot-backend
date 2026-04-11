"""Risk/Blocker extraction service for processing deal content.

This service handles the business logic for risk/blocker extraction:
- Validating deal existence and checking content availability
- Passing context to the RiskBlockerExtractionAgent which uses tools to gather ALL data
- Creating AI-suggested risk/blocker records

Follows fresh session pattern:
- Phase 1: Validate deal, check content exists (fresh session, release before LLM)
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
    RiskBlockerExtractionResult,
)
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventType
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.timeline.deal_timeline_sync_service import (
    build_risk_blocker_timeline_event,
)
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.utils.deal_job_utils import (
    count_deal_emails,
    count_deal_meetings,
    get_deal,
    get_organisation_name,
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
    1. Validates deal exists, checks content availability
    2. Creates RiskBlockerExtractionAgent with org_id + deal_id (agent gathers all data via tools)
    3. Creates AI-suggested risk/blocker records from extracted items

    Follows fresh session pattern:
    - Phase 1: Validate deal, check content (fresh session, release before LLM)
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
        self.last_token_usage = None

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

        # Create timeline event for the AI-extracted risk/blocker
        # Note: AI-generated items create timeline events immediately, not on user confirmation
        timeline_event_type = (
            TimelineEventType.RISK_FLAGGED
            if extracted.item_type == RiskBlockerType.RISK
            else TimelineEventType.BLOCKER_ADDED
        )
        timeline_event = build_risk_blocker_timeline_event(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            risk_blocker=item,
            event_type=timeline_event_type,
        )
        session.add(timeline_event)

        return item

    async def run_extraction(
        self,
        deal_id: UUID,
        force: bool = False,
    ) -> RiskBlockerExtractionResult:
        """Run the full extraction process for a deal.

        Follows fresh session pattern:
        - Phase 1: Validate deal, check content (fresh session, release before LLM)
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

        # Phase 1: Validate deal, check content exists (release before LLM)
        self._publish_progress("Loading deal context...", "context", 0.1)

        org_name: str | None = None
        meeting_count: int = 0
        email_count: int = 0

        async for session in acquire_org_async_session(self.organisation_id):
            deal = await get_deal(session, self.organisation_id, deal_id)
            if not deal:
                raise DealNotFoundError(deal_id)

            # Lightweight guard — no point wasting an LLM call with zero content
            meeting_count = await count_deal_meetings(session, self.organisation_id, deal_id)
            email_count = await count_deal_emails(session, self.organisation_id, deal_id)
            if meeting_count == 0 and email_count == 0:
                raise NoContentError(deal_id)
        # Session released

        # Org name lives in public schema — use a master session
        async for master_session in acquire_master_async_session():
            org_name = await get_organisation_name(master_session, self.organisation_id)

        logger.info(
            "Starting risk/blocker extraction for deal %s",
            deal_id,
            extra={
                "event": "risk_blocker_extraction_started",
                "organisation_id": str(self.organisation_id),
                "deal_id": str(deal_id),
            },
        )
        self._publish_progress("Starting AI analysis...", "preparing", 0.2)

        # Phase 2: Call agent (tools acquire sessions on-demand)
        agent = RiskBlockerExtractionAgent(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            publisher=self.publisher,
            org_name=org_name,
        )
        result = await agent.extract()
        self.last_token_usage = agent.last_token_usage

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
            extra={
                "event": "risk_blocker_extraction_completed",
                "organisation_id": str(self.organisation_id),
                "deal_id": str(deal_id),
                "extracted_items": len(result.items),
                "new_items": created_count,
                "duplicate_items": result.duplicate_items_count,
            },
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
            logger.warning(
                "Extraction failed for deal %s: %s",
                deal_id,
                e.message,
                extra={
                    "event": "risk_blocker_extraction_failed",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id),
                    "error_code": e.error_code,
                    "retryable": e.retryable,
                    "error": e.message,
                },
            )
            return {
                "success": False,
                "deal_id": str(deal_id),
                "error": e.message,
                "error_code": e.error_code,
                "retryable": e.retryable,
            }

        except Exception as e:
            logger.exception(
                "Unexpected error during extraction for deal %s",
                deal_id,
                extra={
                    "event": "risk_blocker_extraction_unexpected_error",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id),
                    "error": str(e),
                },
            )
            return {
                "success": False,
                "deal_id": str(deal_id),
                "error": str(e),
                "error_code": "unexpected_error",
                "retryable": True,
            }
