"""Deal insight services module.

Services for deal-level AI/agent workflows:
- Deal summary: Comprehensive deal analysis across all meetings
- Stakeholder analysis: AI-powered analysis of stakeholder stance, decision power, etc.
- Risk/blocker extraction: AI extraction of risks and blockers from deal communications

Each service follows fresh session pattern:
- Phase 1: Load context (fresh session, release before LLM)
- Phase 2: Call agent (agent manages its own sessions via tools)
- Phase 3: Save result (fresh session)
"""

from src.workers.deal_worker.services.deal_insight_services.deal_summary_service import (
    DealNotFoundError,
    DealSummaryError,
    DealSummaryService,
    NoMeetingsError,
)
from src.workers.deal_worker.services.deal_insight_services.risk_blocker_extraction_service import (
    NoContentError,
    RiskBlockerExtractionError,
    RiskBlockerExtractionService,
)
from src.workers.deal_worker.services.deal_insight_services.stakeholder_analysis_service import (
    StakeholderAnalysisService,
)

__all__ = [
    # Deal summary
    "DealNotFoundError",
    "DealSummaryError",
    "DealSummaryService",
    "NoMeetingsError",
    # Stakeholder analysis
    "StakeholderAnalysisService",
    # Risk/blocker extraction
    "NoContentError",
    "RiskBlockerExtractionError",
    "RiskBlockerExtractionService",
]
