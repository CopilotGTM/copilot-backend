from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScheduledJob:
    """Configuration for a fixed-schedule integrations trigger."""

    name: str
    task_name: str
    schedule: str
    max_lag_seconds: int
    queue: str = "integrations"
    routing_key: str = "integrations"
    kwargs: dict[str, Any] = field(default_factory=dict)


SCHEDULED_JOBS: dict[str, ScheduledJob] = {
    "refresh-tokens": ScheduledJob(
        name="refresh-tokens",
        task_name="integrations.refresh_tokens",
        schedule="0 */4 * * *",
        max_lag_seconds=60 * 60 * 6,
        kwargs={"buffer_hours": 2},
    ),
    "sync-messaging-channels-all-orgs": ScheduledJob(
        name="sync-messaging-channels-all-orgs",
        task_name="integrations.sync_messaging_channels_all_orgs",
        schedule="0 6 * * *",
        max_lag_seconds=60 * 60 * 30,
    ),
    "sync-deal-messages-all-orgs": ScheduledJob(
        name="sync-deal-messages-all-orgs",
        task_name="integrations.sync_deal_messages_all_orgs",
        schedule="*/30 * * * *",
        max_lag_seconds=60 * 60,
    ),
    "sync-calendar-events": ScheduledJob(
        name="sync-calendar-events",
        task_name="integrations.sync_calendar_all_orgs",
        schedule="*/15 * * * *",
        max_lag_seconds=60 * 30,
    ),
    "sync-email-all-orgs": ScheduledJob(
        name="sync-email-all-orgs",
        task_name="integrations.sync_email_all_orgs",
        schedule="*/30 * * * *",
        max_lag_seconds=60 * 60,
    ),
    "sync-hubspot-deal-schema-all-orgs": ScheduledJob(
        name="sync-hubspot-deal-schema-all-orgs",
        task_name="integrations.sync_hubspot_deal_schema_all_orgs",
        schedule="0 2 */14 * *",
        max_lag_seconds=60 * 60 * 48,
    ),
    "sync-salesforce-opportunity-schema-all-orgs": ScheduledJob(
        name="sync-salesforce-opportunity-schema-all-orgs",
        task_name="integrations.sync_salesforce_opportunity_schema_all_orgs",
        schedule="0 3 */14 * *",
        max_lag_seconds=60 * 60 * 48,
    ),
}


def get_scheduled_job(job_name: str) -> ScheduledJob:
    """Return the configured schedule definition for a named integrations job."""

    return SCHEDULED_JOBS[job_name]
