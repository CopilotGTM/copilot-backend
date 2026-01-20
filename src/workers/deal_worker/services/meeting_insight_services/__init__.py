"""Meeting insight services module.

Services for meeting-level AI/agent workflows:
- Meeting prep (briefing): Pre-meeting preparation insights
- Meeting summary: Post-meeting summary generation

Each service follows fresh session pattern:
- Phase 1: Load context (fresh session, release before LLM)
- Phase 2: Call agent (agent manages its own session)
- Phase 3: Save result (fresh session)
"""

from src.workers.deal_worker.services.meeting_insight_services.meeting_prep_service import (
    MeetingPrepService,
    PrepPipelineResult,
)
from src.workers.deal_worker.services.meeting_insight_services.meeting_summary_service import (
    MeetingSummaryService,
    NonRetryableJobError,
    PipelineResult,
    wrap_generated_content,
)

__all__ = [
    "MeetingPrepService",
    "MeetingSummaryService",
    "NonRetryableJobError",
    "PipelineResult",
    "PrepPipelineResult",
    "wrap_generated_content",
]
