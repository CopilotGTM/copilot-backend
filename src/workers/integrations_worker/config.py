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
            # Teams channels periodic sync (DEPRECATED - use three-tier tasks below)
            "integrations.sync_teams_channels": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            # Deal channel messages sync (DEPRECATED - use three-tier tasks below)
            "integrations.trigger_deal_channel_messages_sync_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_deal_channel_messages": {
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
            # Calendar events periodic sync (DEPRECATED - use three-tier tasks below)
            "integrations.sync_calendar_events": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            # Calendar sync tasks (three-tier architecture)
            "integrations.sync_calendar_all_orgs": {
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

        # Celery Beat schedule for periodic tasks
        # Schedule designed for production CRM sync:
        # - Deals & Notes (Activities): Every 3 hours - most critical, changes frequently
        # - Contacts: Every 6 hours - moderate change frequency
        # - Users, Pipelines, Tasks, Companies: Daily - reference data, low change frequency
        self.beat_schedule = {
            # =================================================================
            # CRM SYNC SCHEDULES (Production)
            # =================================================================
            # CRM Users sync (daily at 00:00) - reference data, sync first
            # Must run before other syncs for owner enrichment to work
            "sync-crm-users-all-orgs": {
                "task": "integrations.sync_crm_users_all_orgs",
                "schedule": crontab(minute=0, hour=0),  # Daily at midnight
                "kwargs": {},
                "options": {
                    "queue": "integrations",
                    "routing_key": "integrations",
                },
            },
            # # CRM Pipelines sync (daily at 00:15) - reference data
            # # Must run before deals for pipeline enrichment to work
            # "sync-crm-pipelines-all-orgs": {
            #     "task": "integrations.sync_crm_pipelines_all_orgs",
            #     "schedule": crontab(minute=15, hour=0),  # Daily at 00:15
            #     "kwargs": {},
            #     "options": {
            #         "queue": "integrations",
            #         "routing_key": "integrations",
            #     },
            # },
            # CRM Companies sync (daily at 00:30)
            "sync-crm-companies-all-orgs": {
                "task": "integrations.sync_crm_companies_all_orgs",
                "schedule": crontab(minute=30, hour=0),  # Daily at 00:30
                "kwargs": {},
                "options": {
                    "queue": "integrations",
                    "routing_key": "integrations",
                },
            },
            # CRM Contacts sync (every 6 hours at :00)
            "sync-crm-contacts-all-orgs": {
                "task": "integrations.sync_crm_contacts_all_orgs",
                "schedule": crontab(minute=0, hour="0,6,12,18"),  # Every 6 hours
                "kwargs": {},
                "options": {
                    "queue": "integrations",
                    "routing_key": "integrations",
                },
            },
            # CRM Deals sync (every 3 hours at :30) - most critical
            "sync-crm-deals-all-orgs": {
                "task": "integrations.sync_crm_deals_all_orgs",
                "schedule": crontab(minute=30, hour="0,3,6,9,12,15,18,21"),  # Every 3 hours
                "kwargs": {},
                "options": {
                    "queue": "integrations",
                    "routing_key": "integrations",
                },
            },
            # CRM Activities/Notes sync (every 3 hours at :45)
            "sync-crm-activities-all-orgs": {
                "task": "integrations.sync_crm_activities_all_orgs",
                "schedule": crontab(minute=45, hour="0,3,6,9,12,15,18,21"),  # Every 3 hours
                "kwargs": {},
                "options": {
                    "queue": "integrations",
                    "routing_key": "integrations",
                },
            },
            # =================================================================
            # NON-CRM SYNC SCHEDULES (Disabled - enable as needed)
            # =================================================================
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
            # Messaging channels sync (three-tier: Tier 1 triggers Tier 2 for each org)
            # "sync-messaging-channels-all-orgs": {
            #     "task": "integrations.sync_messaging_channels_all_orgs",  # Three-tier: Tier 1
            #     "schedule": crontab(minute="*/180"),  # Run every 3 hours
            #     "kwargs": {},  # No args = sync all organizations
            #     "options": {
            #         "queue": "integrations",
            #         "routing_key": "integrations",
            #     },
            # },
            # # Deal messages sync (three-tier: Tier 1 triggers Tier 2 for each org)
            # "sync-deal-messages-all-orgs": {
            #     "task": "integrations.sync_deal_messages_all_orgs",  # Three-tier: Tier 1
            #     "schedule": crontab(minute="*/180"),  # Run every 3 hours
            #     "kwargs": {},
            #     "options": {
            #         "queue": "integrations",
            #         "routing_key": "integrations",
            #     },
            # },
            # "sync-calendar-events-all-orgs": {
            #     "task": "integrations.sync_calendar_all_orgs",  # Three-tier: Tier 1
            #     "schedule": crontab(minute="*/30"),  # Run every 30 minutes
            #     "kwargs": {},  # No args = sync all organizations
            #     "options": {
            #         "queue": "integrations",
            #         "routing_key": "integrations",
            #     },
            # },
            # # # Email sync (three-tier: Tier 1 triggers Tier 2 for each org)
            # "sync-email-all-orgs": {
            #     "task": "integrations.sync_email_all_orgs",
            #     "schedule": crontab(minute="*/60"),  # Run every 30 minutes
            #     "kwargs": {},  # No args = sync all organizations
            #     "options": {
            #         "queue": "integrations",
            #         "routing_key": "integrations",
            #     },
            # },
        }
