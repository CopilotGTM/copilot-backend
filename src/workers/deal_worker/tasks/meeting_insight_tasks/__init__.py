"""Meeting insight tasks module.

Tasks for meeting-related AI/agent workflows:
- Meeting prep (briefing): Pre-meeting preparation insights
- Meeting summary: Post-meeting summary generation (requires transcript)
"""

from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_prep_task import (
    process_meeting_prep,
)
from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
    process_meeting_summary,
)

__all__ = ["process_meeting_prep", "process_meeting_summary"]
