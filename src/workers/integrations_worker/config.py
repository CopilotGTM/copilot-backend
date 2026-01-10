from celery.schedules import crontab
from kombu import Exchange, Queue

from src.workers.base.worker_config import BaseWorkerConfig


class IntegrationsWorkerConfig(BaseWorkerConfig):
    """Configuration for the Integrations Worker."""

    def __init__(self):
        super().__init__()

        # Declare a dedicated direct exchange for integrations
        integrations_exchange = Exchange("integrations", type="direct")

        self.task_default_exchange = "integrations"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "integrations"
        self.task_default_routing_key = "integrations"

        self.task_queues = (
            Queue(
                "integrations",
                exchange=integrations_exchange,
                routing_key="integrations",
            ),
            Queue(
                "integrations_priority",
                exchange=integrations_exchange,
                routing_key="integrations_priority",
            ),
        )

        self.task_routes = {
            # Token refresh tasks
            "integrations.refresh_expiring_tokens": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.refresh_single_integration": {
                "queue": "integrations_priority",  # Priority queue for manual refresh
                "routing_key": "integrations_priority",
            },
            # Data sync tasks (to be implemented in Step 5)
            "integrations.sync_integration": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_all_active_integrations": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            # Generic data sync entry
            "integrations.sync_integration_data": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            # Teams channels periodic sync
            "integrations.sync_teams_channels": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            # Deal channel messages sync
            "integrations.trigger_deal_channel_messages_sync_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_deal_channel_messages": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            # Calendar events periodic sync
            "integrations.sync_calendar_events": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            # Email sync tasks (three-tier architecture)
            "integrations.sync_email_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_email_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_email_integration": {
                "queue": "integrations_priority",  # Priority queue for manual triggers
                "routing_key": "integrations_priority",
            },
        }

        # Celery Beat schedule for periodic tasks
        self.beat_schedule = {
            # OAuth token refresh - DISABLED until tested
            # "refresh-expiring-oauth-tokens": {
            #     "task": "integrations.refresh_expiring_tokens",
            #     "schedule": crontab(minute=0),  # Run every hour at minute 0
            #     "kwargs": {"buffer_hours": 2},  # Refresh tokens expiring within 2 hours
            #     "options": {
            #         "queue": "integrations",
            #         "routing_key": "integrations",
            #     },
            # },
            "sync-teams-channels-all-orgs": {
                "task": "integrations.sync_teams_channels",
                "schedule": crontab(minute=0, hour="*/3"),  # Run every 3 hours
                "kwargs": {},  # No args = sync all organizations
                "options": {
                    "queue": "integrations",
                    "routing_key": "integrations",
                },
            },
            "sync-deal-channel-messages-all-orgs": {
                "task": "integrations.trigger_deal_channel_messages_sync_all_orgs",
                "schedule": crontab(minute=30),  # Run every hour at :30
                "kwargs": {},
                "options": {
                    "queue": "integrations",
                    "routing_key": "integrations",
                },
            },
            "sync-calendar-events-all-orgs": {
                "task": "integrations.sync_calendar_events",
                "schedule": crontab(minute="*/30"),  # Run every 30 minutes
                "kwargs": {},  # No args = sync all organizations
                "options": {
                    "queue": "integrations",
                    "routing_key": "integrations",
                },
            },
            # Email sync (three-tier: Tier 1 triggers Tier 2 for each org)
            "sync-email-all-orgs": {
                "task": "integrations.sync_email_all_orgs",
                "schedule": crontab(minute="*/30"),  # Run every 30 minutes
                "kwargs": {},  # No args = sync all organizations
                "options": {
                    "queue": "integrations",
                    "routing_key": "integrations",
                },
            },
            # Data sync schedule (to be uncommented in Step 5)
            # "sync-all-integrations": {
            #     "task": "integrations.sync_all_active_integrations",
            #     "schedule": crontab(minute=0),  # Run every hour
            #     "options": {
            #         "queue": "integrations",
            #         "routing_key": "integrations",
            #     },
            # },
        }
