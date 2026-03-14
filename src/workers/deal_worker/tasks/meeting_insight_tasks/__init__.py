"""Meeting insight tasks module.

Tasks for meeting-related AI/agent workflows:
- Meeting prep (briefing): Pre-meeting preparation insights
- Meeting summary: Post-meeting summary generation (requires transcript)
- Transcript upload: Manual transcript file upload + text extraction
"""

from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_prep_task import (
    process_meeting_prep,
)
from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
    process_meeting_summary,
)
from src.workers.deal_worker.tasks.meeting_insight_tasks.transcript_upload_task import (
    process_transcript_upload,
)

__all__ = ["process_meeting_prep", "process_meeting_summary", "process_transcript_upload"]
