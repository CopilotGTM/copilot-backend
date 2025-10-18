from kombu import Queue

from src.workers.base.worker_config import BaseWorkerConfig


class ContentWorkerConfig(BaseWorkerConfig):
    """Configuration for the Content Worker."""

    def __init__(self):
        super().__init__()
        self.task_queues = (
            Queue("content_generation"),
            Queue("deck_generation"),
            Queue("slides"),
        )
        self.task_default_queue = "content_generation"
