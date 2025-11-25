"""CRM service package."""

from src.service.crm.deals_access_service import DealsAccessService
from src.service.crm.meetings_service import MeetingsService, MeetingsServiceError
from src.service.crm.stakeholders_service import (
    StakeholdersService,
    StakeholdersServiceError,
)
from src.service.crm.tasks_access_service import TasksAccessService

__all__ = [
    "DealsAccessService",
    "MeetingsService",
    "MeetingsServiceError",
    "StakeholdersService",
    "StakeholdersServiceError",
    "TasksAccessService",
]
