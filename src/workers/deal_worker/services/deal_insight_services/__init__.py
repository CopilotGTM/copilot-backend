"""Deal insight services module.

Services for deal-level AI/agent workflows:
- Deal summary: Comprehensive deal analysis across all meetings

Each service follows fresh session pattern:
- Phase 1: Load context (fresh session, release before LLM)
- Phase 2: Call agent (agent manages its own session)
- Phase 3: Save result (fresh session)
"""

from src.workers.deal_worker.services.deal_insight_services.deal_summary_service import (
    DealNotFoundError,
    DealSummaryError,
    DealSummaryService,
    NoMeetingsError,
)

__all__ = [
    "DealNotFoundError",
    "DealSummaryError",
    "DealSummaryService",
    "NoMeetingsError",
]
