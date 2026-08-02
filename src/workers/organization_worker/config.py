from kombu import Exchange, Queue

from src.workers.base.worker_config import BaseWorkerConfig


class OrganizationWorkerConfig(BaseWorkerConfig):
    """Configuration for the Organization Worker."""

    def __init__(self):
        super().__init__()
        # Fix: declare a dedicated direct exchange and bind each queue with a routing
        # key matching its queue name. Also add routing_key to task_routes so Celery
        # produces messages with the correct routing key.

        organization_exchange = Exchange("organization", type="direct")

        self.task_default_exchange = "organization"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "organization"
        self.task_default_routing_key = "organization"

        self.task_queues = (
            Queue(
                "organization",
                exchange=organization_exchange,
                routing_key="organization",
            ),
            Queue("admin", exchange=organization_exchange, routing_key="admin"),
        )

        self.task_routes = {
            "organization.create": {
                "queue": "organization",
                "routing_key": "organization",
            },
            "organization.signup.provision": {
                "queue": "organization",
                "routing_key": "organization",
            },
            "organization.research_kai_training": {
                "queue": "organization",
                "routing_key": "organization",
            },
            "organization.test_task": {
                "queue": "organization",
                "routing_key": "organization",
            },
            "organization.ping": {
                "queue": "organization",
                "routing_key": "organization",
            },
        }
