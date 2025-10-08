from kombu import Queue, Exchange
from celery.schedules import crontab
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
        }

        # Celery Beat schedule for periodic tasks
        self.beat_schedule = {
            "refresh-expiring-oauth-tokens": {
                "task": "integrations.refresh_expiring_tokens",
                "schedule": crontab(minute=0),  # Run every hour at minute 0
                "kwargs": {"buffer_hours": 2},  # Refresh tokens expiring within 2 hours
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
