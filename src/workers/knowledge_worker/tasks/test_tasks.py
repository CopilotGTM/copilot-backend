"""Test tasks for debugging worker connectivity."""

from src.workers.knowledge_worker.celery_app import app
from src.infra.utils.logger import get_logger

logger = get_logger("knowledge_test_tasks")


@app.task(name="knowledge.test_task", bind=True)
def test_task(self, message: str = "Hello from knowledge worker!"):
    """Simple test task to verify worker connectivity."""
    logger.info(f"Test task executed with message: {message}")
    print(f"Test task executed with message: {message}")
    return {"status": "success", "message": message, "task_id": self.request.id}


@app.task(name="knowledge.ping", bind=True)
def ping_task(self):
    """Simple ping task."""
    logger.info("Ping task executed")
    print("Ping task executed")
    return {"status": "pong", "task_id": self.request.id}
