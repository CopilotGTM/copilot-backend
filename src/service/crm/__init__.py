"""CRM service package."""

from src.service.crm.deal_job_service import DealJobService, DealJobServiceError
from src.service.crm.deal_job_sse_service import DealJobSSEService
from src.service.crm.deal_meeting_job_service import (
    DealMeetingJobService,
    DealMeetingJobServiceError,
)
from src.service.crm.deal_timeline_service import (
    DealTimelineService,
    DealTimelineServiceError,
)
from src.service.crm.deal_timeline_sync_service import (
    DealTimelineSyncService,
    build_email_timeline_event,
    build_meeting_timeline_event,
)
from src.service.crm.deals_access_service import DealsAccessService
from src.service.crm.meetings_service import MeetingsService, MeetingsServiceError
from src.service.crm.stakeholders_service import (
    StakeholdersService,
    StakeholdersServiceError,
)
from src.service.crm.tasks_access_service import TasksAccessService

__all__ = [
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
    "MeetingsService",
    "MeetingsServiceError",
    "StakeholdersService",
    "StakeholdersServiceError",
    "TasksAccessService",
]
