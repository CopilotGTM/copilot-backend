"""CRM service package.

Note: The following services have FastAPI/API schema dependencies and are NOT
exported from this package to avoid breaking workers:
- MeetingsService (from meetings/meetings_service.py)
- StakeholdersService (from insights/stakeholders_service.py)
- RiskBlockerService (from insights/risk_blocker_service.py)

Import these directly if needed:
    from src.service.crm.meetings.meetings_service import MeetingsService
"""

from src.service.crm.access.tasks_access_service import TasksAccessService
from src.service.crm.deals.read_service import DealReadService
from src.service.crm.jobs.deal_job_service import DealJobService, DealJobServiceError
from src.service.crm.jobs.deal_job_sse_service import DealJobSSEService
from src.service.crm.jobs.deal_meeting_job_service import (
    DealMeetingJobService,
    DealMeetingJobServiceError,
)
from src.service.crm.timeline.deal_timeline_service import (
    DealTimelineService,
    DealTimelineServiceError,
)
from src.service.crm.timeline.deal_timeline_sync_service import (
    DealTimelineSyncService,
    build_email_timeline_event,
    build_meeting_timeline_event,
)

__all__ = [
    "DealReadService",
    "DealsAccessService",
    "DealJobService",
    "DealJobServiceError",
    "DealMeetingJobService",
    "DealMeetingJobServiceError",
    "DealJobSSEService",
    "DealTimelineService",
    "DealTimelineServiceError",
    "DealTimelineSyncService",
    "build_email_timeline_event",
    "build_meeting_timeline_event",
    "TasksAccessService",
]

DealsAccessService = DealReadService
