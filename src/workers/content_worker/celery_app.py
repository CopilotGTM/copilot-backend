import os

# Set minimal DB pool settings for worker process
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"

from src.workers.base.celery_base import create_celery_app
from src.workers.content_worker.config import ContentWorkerConfig

# Initialize the configuration for the Content Worker
config = ContentWorkerConfig()

# Define the tasks module for auto-discovery
tasks_module = "src.workers.content_worker.tasks"

# Create the Celery app for the Content Worker
app = create_celery_app("content_worker", config, tasks_module)
