"""Public task exports for the deal worker.

This module is the import surface Celery loads for autodiscovery, and maps all
deal-worker tasks to stable names for queue dispatch. Keep imports explicit so
worker startup fails loudly if a task module stops importing.
"""

from .dashboard_snapshot_tasks import (
    refresh_sales_snapshot_org_task,
    refresh_sales_snapshots_all_orgs_task,
)
from .deal_asset_email_attachment_tasks import (
    extract_email_attachments_all_orgs_task,
    extract_email_attachments_deal_task,
    extract_email_attachments_org_task,
)
from .deal_asset_post_upload_task import deal_asset_post_upload
from .deal_insight_tasks import (
    analyze_deal_stakeholders_task,
    extract_deal_risks_blockers_task,
    process_deal_summary_job,
    trigger_extraction_all_orgs,
)
from .deal_mappers import (
    discover_calendar_contacts_accounts_task,
    exclude_timeline_event_task,
    match_calendar_to_deals_all_orgs_task,
    match_calendar_to_deals_org_task,
    match_calendar_to_deals_task,
    match_email_to_deals_org_task,
    match_email_to_deals_task,
    sync_all_deals_stakeholders_org_task,
    sync_deal_stakeholders_task,
    sync_deal_timeline_task,
    sync_org_timeline_task,
    trigger_timeline_sync_all_orgs_task,
)

# Meeting bot tasks are lightweight orchestration around calendar events,
# so they are exported alongside the other meeting workflows.
from .meeting_bot_tasks import (
    process_meeting_recording_webhook_task,
    send_meeting_recording_task,
    send_unsent_meeting_recordings_org_task,
    trigger_send_unsent_meeting_recordings_all_orgs_task,
)
from .meeting_insight_tasks import (
    process_meeting_prep,
    process_meeting_summary,
    send_meeting_summary_ready_notification_task,
)
from .stale_job_cleanup_tasks import (
    cleanup_stale_deal_jobs_all_orgs_task,
    cleanup_stale_deal_jobs_org_task,
)
from .transcript_fetch_tasks import (
    fetch_meeting_transcripts_org_task,
    trigger_transcript_fetch_all_orgs_task,
)

__all__ = [
    # Deal mappers - Task 1: Stakeholder Sync
    "sync_deal_stakeholders_task",
    "sync_all_deals_stakeholders_org_task",
    # Deal mappers - Task 2: Calendar Matching
    "discover_calendar_contacts_accounts_task",
    "match_calendar_to_deals_all_orgs_task",
    "match_calendar_to_deals_org_task",
    "match_calendar_to_deals_task",
    # Deal mappers - Task 3: Email Matching
    "match_email_to_deals_org_task",
    "match_email_to_deals_task",
    # Meeting insight tasks (meeting-level)
    "process_meeting_prep",
    "process_meeting_summary",
    "send_meeting_summary_ready_notification_task",
    "process_meeting_recording_webhook_task",
    "send_meeting_recording_task",
    "send_unsent_meeting_recordings_org_task",
    "trigger_send_unsent_meeting_recordings_all_orgs_task",
    # Deal summary tasks (deal-level)
    "process_deal_summary_job",
    # Stakeholder analysis tasks
    "analyze_deal_stakeholders_task",
    # Risk/blocker extraction tasks
    "extract_deal_risks_blockers_task",
    "trigger_extraction_all_orgs",
    # Timeline tasks
    "sync_deal_timeline_task",
    "sync_org_timeline_task",
    "exclude_timeline_event_task",
    "trigger_timeline_sync_all_orgs_task",
    # Transcript fetch tasks
    "fetch_meeting_transcripts_org_task",
    "trigger_transcript_fetch_all_orgs_task",
    # Stale job cleanup tasks
    "cleanup_stale_deal_jobs_all_orgs_task",
    "cleanup_stale_deal_jobs_org_task",
    # Deal asset tasks
    "deal_asset_post_upload",
    "extract_email_attachments_all_orgs_task",
    "extract_email_attachments_org_task",
    "extract_email_attachments_deal_task",
    "refresh_sales_snapshots_all_orgs_task",
    "refresh_sales_snapshot_org_task",
]
