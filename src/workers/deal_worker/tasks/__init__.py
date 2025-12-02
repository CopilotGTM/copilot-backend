"""
Tasks for the deal worker.
"""

from .deal_meeting_tasks import process_deal_meeting_job
from .deal_tasks import (
    process_deal_meetings_task,
    process_single_deal_task,
    trigger_deal_meeting_processing_all_orgs,
)
from .stakeholder_analysis_tasks import (
    analyze_deal_stakeholders_task,
    analyze_org_stakeholders_task,
    trigger_stakeholder_analysis_all_orgs,
)
from .stakeholder_tasks import sync_stakeholders_from_meeting_task

__all__ = [
    # Deal tasks
    "process_deal_meeting_job",
    "process_deal_meetings_task",
    "process_single_deal_task",
    "trigger_deal_meeting_processing_all_orgs",
    # Stakeholder sync tasks
    "sync_stakeholders_from_meeting_task",
    # Stakeholder analysis tasks
    "analyze_deal_stakeholders_task",
    "analyze_org_stakeholders_task",
    "trigger_stakeholder_analysis_all_orgs",
]
