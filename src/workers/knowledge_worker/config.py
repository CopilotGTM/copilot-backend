from kombu import Exchange, Queue

from src.workers.base.worker_config import BaseWorkerConfig


class KnowledgeWorkerConfig(BaseWorkerConfig):
    """Configuration for the Knowledge Worker."""

    def __init__(self):
        super().__init__()

        # 30 minutes for hard limit, 28 minutes for soft limit
        self.task_time_limit = 1800  # 30 minutes (30 * 60 seconds)
        self.task_soft_time_limit = 1680  # 28 minutes (28 * 60 seconds)

        knowledge_exchange = Exchange("knowledge", type="direct")

        self.task_default_exchange = "knowledge"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "knowledge_upload"
        self.task_default_routing_key = "knowledge_upload"

        self.task_queues = (
            Queue(
                "knowledge_extract",
                exchange=knowledge_exchange,
                routing_key="knowledge_extract",
            ),
            Queue(
                "knowledge_upload",
                exchange=knowledge_exchange,
                routing_key="knowledge_upload",
            ),
        )

        self.task_routes = {
            "knowledge.document_extraction": {
                "queue": "knowledge_extract",
                "routing_key": "knowledge_extract",
            },
            "knowledge.post_upload_process": {
                "queue": "knowledge_upload",
                "routing_key": "knowledge_upload",
            },
            "knowledge.test_task": {
                "queue": "knowledge_upload",
                "routing_key": "knowledge_upload",
            },
            "knowledge.ping": {
                "queue": "knowledge_upload",
                "routing_key": "knowledge_upload",
            },
            "knowledge.delete_source": {
                "queue": "knowledge_upload",
                "routing_key": "knowledge_upload",
            },
            "knowledge.cleanup_stale_jobs_all_orgs": {
                "queue": "knowledge_upload",
                "routing_key": "knowledge_upload",
            },
            "knowledge.cleanup_stale_jobs_org": {
                "queue": "knowledge_upload",
                "routing_key": "knowledge_upload",
            },
            "knowledge.cleanup_stale_connections": {
                "queue": "knowledge_upload",
                "routing_key": "knowledge_upload",
            },
        }

        # Fixed knowledge schedules are owned by Kubernetes CronJobs.
        self.beat_schedule = {}
