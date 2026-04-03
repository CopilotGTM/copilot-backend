from celery.schedules import crontab
from kombu import Exchange, Queue

from src.workers.base.worker_config import BaseWorkerConfig

# ── Task execution limits ──────────────────────────────────────────────
# Single source of truth for all integrations worker task parameters.
#
# Rules:
#   1. hard = soft + 60  (gives the soft handler time to log and exit)
#   2. Tier 1 (coordinators): short — just query orgs and dispatch
#   3. Tier 2 (per-org): scaled to workload
#   4. Tier 3 (manual/single): 5-min cap — user is waiting
#   5. Coordinators never retry; per-org retries 2-3x; manual retries 3x
#
# To tune a limit: change it HERE, not in the task file.

TASK_LIMITS: dict[str, dict] = {
    # ── Tier 1: coordinators (query approved orgs → fan out) ──
    "integrations.sync_calendar_all_orgs": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    "integrations.sync_email_all_orgs": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    "integrations.sync_messaging_channels_all_orgs": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    "integrations.sync_deal_messages_all_orgs": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    "integrations.refresh_tokens": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    # ── Tier 2: per-org (external API calls, pagination, persistence) ──
    "integrations.sync_calendar_org": {
        "soft": 600,
        "hard": 660,
        "retries": 2,
        "retry_delay": 120,
    },
    "integrations.sync_email_org": {
        "soft": 1800,
        "hard": 1860,
        "retries": 2,
        "retry_delay": 120,
    },
    "integrations.sync_messaging_channels_org": {
        "soft": 900,
        "hard": 960,
        "retries": 2,
        "retry_delay": 120,
    },
    "integrations.sync_deal_messages_org": {
        "soft": 600,
        "hard": 660,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.refresh_tokens_org": {
        "soft": 300,
        "hard": 360,
        "retries": 2,
        "retry_delay": 120,
    },
    # ── Tier 3: single integration (user-triggered, fast feedback) ──
    "integrations.sync_calendar_integration": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_email_integration": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_messaging_channels_integration": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_deal_messages_integration": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.refresh_token": {
        "soft": 120,
        "hard": 180,
        "retries": 3,
        "retry_delay": 60,
    },
}

CALENDAR_SYNC_TASK = "integrations.sync_calendar_all_orgs"


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
            # Token refresh tasks (three-tier architecture)
            "integrations.refresh_tokens": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.refresh_tokens_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.refresh_token": {
                "queue": "integrations_priority",  # Priority queue for single refresh
                "routing_key": "integrations_priority",
            },
            # Generic data sync task (used by three-tier tasks)
            "integrations.sync_integration": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            # Generic data sync entry
            "integrations.sync_integration_data": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            # Messaging channels sync tasks (three-tier architecture)
            "integrations.sync_messaging_channels_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_messaging_channels_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_messaging_channels_integration": {
                "queue": "integrations_priority",  # Priority queue for manual triggers
                "routing_key": "integrations_priority",
            },
            # Deal messages sync tasks (three-tier architecture)
            "integrations.sync_deal_messages_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_deal_messages_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_deal_messages_integration": {
                "queue": "integrations_priority",  # Priority queue for manual triggers
                "routing_key": "integrations_priority",
            },
            # Calendar sync tasks (three-tier architecture)
            CALENDAR_SYNC_TASK: {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_calendar_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_calendar_integration": {
                "queue": "integrations_priority",  # Priority queue for manual triggers
                "routing_key": "integrations_priority",
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
            # CRM sync tasks (HubSpot, Salesforce) deprecated — moved to deprecated/workers/
        }

        # Celery Beat schedule for periodic tasks.
        # Organization:
        #   1. Maintenance (token refresh, shared housekeeping)
        #   2. Communication/messaging workloads (US business-hour focus)
        # Note: CRM sync (HubSpot, Salesforce) deprecated — schedule removed

        _maintenance_schedule = {
            "refresh-tokens": {
                "task": "integrations.refresh_tokens",
                "schedule": crontab(minute=0, hour="*/4"),  # Every 4 hours
                "kwargs": {"buffer_hours": 2},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
        }

        _communication_schedule = {
            # Channel sync: Once daily (Microsoft Graph polling limit)
            "sync-messaging-channels-all-orgs": {
                "task": "integrations.sync_messaging_channels_all_orgs",
                "schedule": crontab(minute=0, hour=6),  # Once daily at 6 AM UTC
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
            "sync-deal-messages-all-orgs": {
                "task": "integrations.sync_deal_messages_all_orgs",
                "schedule": crontab(minute="*/15", hour="12-23"),
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
            # Calendar cadence split between day/night
            "sync-calendar-events-daytime": {
                "task": CALENDAR_SYNC_TASK,
                "schedule": crontab(minute="*/15", hour="12-23"),
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
            "sync-calendar-events-nighttime": {
                "task": CALENDAR_SYNC_TASK,
                "schedule": crontab(minute=0, hour="0-11"),
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
            # Email runs every 30 minutes around the clock
            "sync-email-all-orgs": {
                "task": "integrations.sync_email_all_orgs",
                "schedule": crontab(minute="0,30"),
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
        }

        self.beat_schedule = {
            **_maintenance_schedule,
            **_communication_schedule,
        }
