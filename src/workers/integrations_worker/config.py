"""Integration flow logic for config."""

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
    # ── Tier 3: single integration (user-triggered, includes initial sync) ──
    "integrations.sync_calendar_integration": {
        "soft": 900,
        "hard": 960,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_email_integration": {
        "soft": 900,
        "hard": 960,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_messaging_channels_integration": {
        "soft": 900,
        "hard": 960,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_deal_messages_integration": {
        "soft": 900,
        "hard": 960,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.refresh_token": {
        "soft": 120,
        "hard": 180,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.ensure_microsoft_graph_subscription": {
        "soft": 120,
        "hard": 180,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.renew_microsoft_graph_subscriptions_all": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    "integrations.ensure_google_subscription": {
        "soft": 120,
        "hard": 180,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.renew_google_subscriptions_all": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    "integrations.sync_hubspot_deal_schema": {
        "soft": 900,
        "hard": 960,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_hubspot_deal_schema_all_orgs": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    "integrations.sync_hubspot_core_incremental_all_orgs": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    "integrations.sync_hubspot_core_incremental": {
        "soft": 600,
        "hard": 660,
        "retries": 2,
        "retry_delay": 60,
    },
    "integrations.reconcile_hubspot_deletions_all_orgs": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    "integrations.reconcile_hubspot_deletions": {
        "soft": 1800,
        "hard": 1860,
        "retries": 2,
        "retry_delay": 120,
    },
    "integrations.sync_salesforce_opportunity_schema": {
        "soft": 900,
        "hard": 960,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_salesforce_opportunity_schema_all_orgs": {
        "soft": 120,
        "hard": 180,
        "retries": 0,
    },
    # ── HubSpot record sync (phase 2: pipelines + stages) ──
    "integrations.sync_hubspot_pipelines": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_salesforce_pipelines": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    # ── HubSpot record sync (phase 3: users / owners, matched-only) ──
    "integrations.sync_hubspot_users": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_hubspot_tasks": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_hubspot_notes": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_salesforce_users": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_salesforce_companies": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_salesforce_contacts": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_salesforce_deals": {
        "soft": 600,
        "hard": 720,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_salesforce_tasks": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.sync_salesforce_notes": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    # ── HubSpot record sync (phase 4: companies / accounts) ──
    "integrations.sync_hubspot_companies": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    # ── HubSpot record sync (phase 5: contacts) ──
    "integrations.sync_hubspot_contacts": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    "integrations.process_hubspot_webhook_event": {
        "soft": 300,
        "hard": 360,
        "retries": 3,
        "retry_delay": 60,
    },
    # ── HubSpot record sync (phase 6: deals fact table) ──
    # Larger window than the dimensions: deal volumes per portal are typically
    # the highest of any object, and we request `propertiesWithHistory` which
    # halves HubSpot's page size to 50.
    "integrations.sync_hubspot_deals": {
        "soft": 600,
        "hard": 660,
        "retries": 3,
        "retry_delay": 60,
    },
    # Chain terminators — short, idempotent, owns initial_sync_status writes.
    "integrations.mark_hubspot_initial_sync_succeeded": {
        "soft": 30,
        "hard": 60,
        "retries": 2,
        "retry_delay": 30,
    },
    "integrations.mark_hubspot_initial_sync_failed": {
        "soft": 30,
        "hard": 60,
        "retries": 2,
        "retry_delay": 30,
    },
    "integrations.mark_salesforce_initial_sync_succeeded": {
        "soft": 30,
        "hard": 60,
        "retries": 2,
        "retry_delay": 30,
    },
    "integrations.mark_salesforce_initial_sync_failed": {
        "soft": 30,
        "hard": 60,
        "retries": 2,
        "retry_delay": 30,
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
            "integrations.ensure_microsoft_graph_subscription": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.renew_microsoft_graph_subscriptions_all": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.ensure_google_subscription": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.renew_google_subscriptions_all": {
                "queue": "integrations",
                "routing_key": "integrations",
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
            # CRM schema sync tasks (HubSpot)
            "integrations.sync_hubspot_deal_schema_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_hubspot_deal_schema": {
                "queue": "integrations_priority",  # Priority queue for manual / post-connect triggers
                "routing_key": "integrations_priority",
            },
            "integrations.sync_hubspot_core_incremental_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_hubspot_core_incremental": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.reconcile_hubspot_deletions_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.reconcile_hubspot_deletions": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_salesforce_opportunity_schema_all_orgs": {
                "queue": "integrations",
                "routing_key": "integrations",
            },
            "integrations.sync_salesforce_opportunity_schema": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            # CRM record sync tasks (HubSpot, phase 2: pipelines + stages)
            "integrations.sync_hubspot_pipelines": {
                "queue": "integrations_priority",  # Co-scheduled with the chain it belongs to
                "routing_key": "integrations_priority",
            },
            "integrations.sync_salesforce_pipelines": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            # CRM record sync tasks (HubSpot, phase 3: users / owners)
            "integrations.sync_hubspot_users": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.sync_hubspot_tasks": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.sync_hubspot_notes": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.sync_salesforce_users": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.sync_salesforce_companies": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.sync_salesforce_contacts": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.sync_salesforce_deals": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.sync_salesforce_tasks": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.sync_salesforce_notes": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            # CRM record sync tasks (HubSpot, phase 4: companies / accounts)
            "integrations.sync_hubspot_companies": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            # CRM record sync tasks (HubSpot, phase 5: contacts)
            "integrations.sync_hubspot_contacts": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.process_hubspot_webhook_event": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            # CRM record sync tasks (HubSpot, phase 6: deals fact table)
            "integrations.sync_hubspot_deals": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.mark_hubspot_initial_sync_succeeded": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.mark_hubspot_initial_sync_failed": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.mark_salesforce_initial_sync_succeeded": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
            "integrations.mark_salesforce_initial_sync_failed": {
                "queue": "integrations_priority",
                "routing_key": "integrations_priority",
            },
        }

        # Fixed integrations schedules are owned by Kubernetes CronJobs.
        self.beat_schedule = {}
