"""Test tasks for debugging chat worker connectivity."""

from src.infra.utils.logger import get_logger
from src.workers.chat_worker.celery_app import app

logger = get_logger("chat_test_tasks")


@app.task(name="chat.test_task", bind=True)
def test_task(self, message: str = "Hello from chat worker!"):
    """Simple test task to verify worker connectivity."""
    logger.info(f"Test task executed with message: {message}")
    print(f"Test task executed with message: {message}")
    return {"status": "success", "message": message, "task_id": self.request.id}


@app.task(name="chat.ping", bind=True)
def ping_task(self):
    """Simple ping task."""
    logger.info("Ping task executed")
    print("Ping task executed")
    return {"status": "pong", "task_id": self.request.id}
