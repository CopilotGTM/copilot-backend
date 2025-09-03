from kombu import Queue, Exchange
from src.workers.base.worker_config import BaseWorkerConfig

class ChatWorkerConfig(BaseWorkerConfig):
    """Configuration for the Chat Worker."""

    def __init__(self):
        super().__init__()
        # Fix: declare a dedicated direct exchange and bind each queue with a routing
        # key matching its queue name. Also add routing_key to task_routes so Celery
        # produces messages with the correct routing key.

        chat_exchange = Exchange("chat", type="direct")

        self.task_default_exchange = "chat"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "attachments"
        self.task_default_routing_key = "attachments"

        self.task_queues = (
            Queue("attachments", exchange=chat_exchange, routing_key="attachments"),
            Queue("chat_processing", exchange=chat_exchange, routing_key="chat_processing"),
            Queue("chat_messages", exchange=chat_exchange, routing_key="chat_messages"),
        )

        self.task_routes = {
            "attachments.post_upload": {"queue": "attachments", "routing_key": "attachments"},
            "chat.test_task": {"queue": "chat_processing", "routing_key": "chat_processing"},
            "chat.ping": {"queue": "chat_processing", "routing_key": "chat_processing"},
            "chat.process_message": {"queue": "chat_messages", "routing_key": "chat_messages"},
        }
