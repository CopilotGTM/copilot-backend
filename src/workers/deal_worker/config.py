from celery.schedules import crontab
from kombu import Exchange, Queue

from src.workers.base.worker_config import BaseWorkerConfig


class DealWorkerConfig(BaseWorkerConfig):
    """Configuration for the Deal Worker."""

    def __init__(self):
        super().__init__()

        deal_exchange = Exchange("deal", type="direct")

        self.task_default_exchange = "deal"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "deal"
        self.task_default_routing_key = "deal"

        self.task_queues = (
            Queue(
                "deal",
                exchange=deal_exchange,
                routing_key="deal",
            ),
            Queue(
                "deal_jobs",
                exchange=deal_exchange,
                routing_key="deal_jobs",
            ),
        )

        self.task_routes = {
            "deal.process_deal_meetings_task": {
                "queue": "deal",
                "routing_key": "deal",
            },
            "deal.trigger_deal_meeting_processing_all_orgs": {
                "queue": "deal",
                "routing_key": "deal",
            },
            "deal.process_single_deal_task": {
                "queue": "deal",
                "routing_key": "deal",
            },
            # All deal meeting job tasks route to the deal_jobs queue
            "deal_meeting_task.process": {
                "queue": "deal_jobs",
                "routing_key": "deal_jobs",
            },
        }

        # Celery Beat schedule for periodic tasks
        self.beat_schedule = {
            "process-deal-meetings-all-orgs": {
                "task": "deal.trigger_deal_meeting_processing_all_orgs",
                "schedule": crontab(minute=0, hour="*/3"),  # Run every 3 hours
                "options": {
                    "queue": "deal",
                    "routing_key": "deal",
                },
            },
        }
