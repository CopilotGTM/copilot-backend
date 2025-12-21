"""
Tasks for the deal worker.
"""

from .deal_email_tasks import (
    process_deal_emails_task,
    trigger_deal_email_processing_all_orgs,
)
from .deal_meeting_tasks import process_deal_meeting_job
from .deal_summary_tasks import process_deal_summary_job
from .deal_tasks import (
    process_deal_meetings_task,
    process_single_deal_task,
    trigger_deal_meeting_processing_all_orgs,
)
from .deal_timeline_tasks import (
    exclude_timeline_event_task,
    sync_deal_timeline_task,
    sync_org_timeline_task,
    trigger_timeline_sync_all_orgs,
)
from .risk_blocker_extraction_tasks import (
    extract_deal_risks_blockers_task,
    trigger_extraction_all_orgs,
)
from .stakeholder_analysis_tasks import (
    analyze_deal_stakeholders_task,
    analyze_org_stakeholders_task,
    trigger_stakeholder_analysis_all_orgs,
)
from .stakeholder_tasks import sync_stakeholders_from_meeting_task

__all__ = [
    # Deal email tasks
    "process_deal_emails_task",
    "trigger_deal_email_processing_all_orgs",
    # Deal meeting tasks (meeting-level)
    "process_deal_meeting_job",
    # Deal summary tasks (deal-level)
    "process_deal_summary_job",
    # Deal tasks
    "process_deal_meetings_task",
    "process_single_deal_task",
    "trigger_deal_meeting_processing_all_orgs",
    # Stakeholder sync tasks
    "sync_stakeholders_from_meeting_task",
    # Stakeholder analysis tasks
    "analyze_deal_stakeholders_task",
    "analyze_org_stakeholders_task",
    "trigger_stakeholder_analysis_all_orgs",
    # Risk/blocker extraction tasks
    "extract_deal_risks_blockers_task",
    "trigger_extraction_all_orgs",
    # Timeline tasks
    "sync_deal_timeline_task",
    "sync_org_timeline_task",
    "exclude_timeline_event_task",
    "trigger_timeline_sync_all_orgs",
]
