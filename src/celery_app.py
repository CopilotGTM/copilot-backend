from celery import Celery
from src.infra.config.configs import Config

# Load configuration and Redis connection details
config = Config()
redis_host, redis_port, redis_password = config.get_redis_client_details
broker_url = f"redis://{redis_host}:{redis_port}/0"
backend_url = broker_url

# Instantiate Celery, including the task modules so they're registered
celery = Celery(
    "technical_deck_worker", broker=broker_url, backend=backend_url, include=["src.tasks.technical_deck", "src.tasks.slide_outline"]
)

# Auto-discover any other tasks in src.tasks
celery.autodiscover_tasks(["src.tasks"])
