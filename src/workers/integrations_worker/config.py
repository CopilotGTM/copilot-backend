from celery.schedules import crontab
from kombu import Exchange, Queue

from src.workers.base.worker_config import BaseWorkerConfig

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
            # CRM Deals sync tasks (three-tier architecture)
            "integrations.sync_crm_deals_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_deals_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_deals_integration": {
                "queue": "integrations_priority",  # Priority queue for manual triggers
                "routing_key": "integrations_priority",
            },
            # CRM Pipelines sync tasks (three-tier architecture)
            "integrations.sync_crm_pipelines_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_pipelines_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_pipelines_integration": {
                "queue": "integrations_priority",  # Priority queue for manual triggers
                "routing_key": "integrations_priority",
            },
            # CRM Contacts sync tasks (three-tier architecture)
            "integrations.sync_crm_contacts_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_contacts_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_contacts_integration": {
                "queue": "integrations_priority",  # Priority queue for manual triggers
                "routing_key": "integrations_priority",
            },
            # CRM Companies sync tasks (three-tier architecture)
            "integrations.sync_crm_companies_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_companies_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_companies_integration": {
                "queue": "integrations_priority",  # Priority queue for manual triggers
                "routing_key": "integrations_priority",
            },
            # CRM Users sync tasks (three-tier architecture)
            "integrations.sync_crm_users_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_users_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_users_integration": {
                "queue": "integrations_priority",  # Priority queue for manual triggers
                "routing_key": "integrations_priority",
            },
            # CRM Activities sync tasks (three-tier architecture)
            "integrations.sync_crm_activities_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_activities_org": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_crm_activities_integration": {
                "queue": "integrations_priority",  # Priority queue for manual triggers
                "routing_key": "integrations_priority",
            },
        }

        # Celery Beat schedule for periodic tasks.
        # Organization:
        #   1. CRM sync (business critical data pipelines)
        #   2. Maintenance (token refresh, shared housekeeping)
        #   3. Communication/messaging workloads (US business-hour focus)

        _crm_schedule = {
            # Reference data — once daily
            "sync-crm-users-all-orgs": {
                "task": "integrations.sync_crm_users_all_orgs",
                "schedule": crontab(minute=0, hour=0),
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
            "sync-crm-pipelines-all-orgs": {
                "task": "integrations.sync_crm_pipelines_all_orgs",
                "schedule": crontab(minute=30, hour=0),
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
            # Medium churn — every hour
            "sync-crm-companies-all-orgs": {
                "task": "integrations.sync_crm_companies_all_orgs",
                "schedule": crontab(minute=25, hour="*"),
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
            "sync-crm-activities-all-orgs": {
                "task": "integrations.sync_crm_activities_all_orgs",
                "schedule": crontab(minute=30, hour="*"),
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
            # High churn — every 15 minutes
            "sync-crm-contacts-all-orgs": {
                "task": "integrations.sync_crm_contacts_all_orgs",
                "schedule": crontab(minute="1,16,31,46"),
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
            "sync-crm-deals-all-orgs": {
                "task": "integrations.sync_crm_deals_all_orgs",
                "schedule": crontab(minute="8,23,38,53"),
                "kwargs": {},
                "options": {"queue": "integrations", "routing_key": "integrations"},
            },
        }

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

        # Migration freeze: disable all integrations worker beat-triggered runs.
        # Manual task dispatch remains available; only scheduled runs are paused.
        self.beat_schedule = {
            # **_crm_schedule,
            # **_maintenance_schedule,
            # **_communication_schedule,
        }
