from kombu import Queue, Exchange
from src.workers.base.worker_config import BaseWorkerConfig

class KnowledgeWorkerConfig(BaseWorkerConfig):
    """Configuration for the Knowledge Worker."""

    def __init__(self):
        super().__init__()
        
        # Set extended time limits for knowledge processing tasks
        # 30 minutes for hard limit, 28 minutes for soft limit
        self.task_time_limit = 1800  # 30 minutes (30 * 60 seconds)
        self.task_soft_time_limit = 1680  # 28 minutes (28 * 60 seconds)
        
        # IMPORTANT:
        # Previously queues were declared without explicit routing keys, so Celery bound
        # every queue to the default direct exchange using the *same* routing key
        # (task_default_queue). That meant messages published with routing_key equal to
        # the queue name (Celery's behaviour when sending via send_task(queue="..."))
        # did not match any binding except for the default queue, causing tasks to
        # appear "stuck" / never consumed on the other queues.
        #
        # Fix: declare a dedicated direct exchange and bind each queue with a routing
        # key matching its queue name. Also add routing_key to task_routes so Celery
        # produces messages with the correct routing key.

        knowledge_exchange = Exchange("knowledge", type="direct")

        self.task_default_exchange = "knowledge"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "knowledge_upload"
        self.task_default_routing_key = "knowledge_upload"

        self.task_queues = (
            Queue("knowledge_extract", exchange=knowledge_exchange, routing_key="knowledge_extract"),
            Queue("knowledge_upload", exchange=knowledge_exchange, routing_key="knowledge_upload"),
        )

        self.task_routes = {
            "knowledge.document_extraction": {"queue": "knowledge_extract", "routing_key": "knowledge_extract"},
            "knowledge.post_upload_process": {"queue": "knowledge_upload", "routing_key": "knowledge_upload"},
            "knowledge.test_task": {"queue": "knowledge_upload", "routing_key": "knowledge_upload"},
            "knowledge.ping": {"queue": "knowledge_upload", "routing_key": "knowledge_upload"},
            "knowledge.delete_source": {"queue": "knowledge_upload", "routing_key": "knowledge_upload"},
        }
