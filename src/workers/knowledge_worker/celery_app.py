from dotenv import load_dotenv

# Load environment variables first
load_dotenv(override=True)

# Debug: Check if environment variables are loaded
import os

import os

# Set minimal DB pool settings for worker process
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"

from src.workers.base.celery_base import create_celery_app
from src.workers.knowledge_worker.config import KnowledgeWorkerConfig
from celery.signals import task_received, task_prerun, task_postrun, task_failure

# Initialize the configuration for the Knowledge Worker
config = KnowledgeWorkerConfig()

# Define the tasks module for auto-discovery
tasks_module = "src.workers.knowledge_worker.tasks"

# Create the Celery app for the Knowledge Worker
app = create_celery_app("knowledge_worker", config, tasks_module)

# Add debug configuration and knowledge-worker specific settings
app.conf.update(
    worker_log_format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    worker_task_log_format='[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s',
    # Additional configuration for handling long-running tasks
    worker_pool_restarts=True,  # Allow worker pool to be restarted if needed
    worker_max_tasks_per_child=50,  # Restart worker process after 50 tasks to prevent memory leaks
    # Result backend settings for long-running tasks
    result_expires=7200,  # Results expire after 2 hours
    result_persistent=True,  # Persist results to backend
)

# Add signal handlers for debugging
@task_received.connect
def task_received_handler(sender=None, headers=None, body=None, **kwargs):
    print(f"Task received: {headers}")

@task_prerun.connect
def task_prerun_handler(task_id=None, task=None, args=None, kwargs=None, **kwds):
    print(f"Task starting: {task.name}({task_id}) with args={args} kwargs={kwargs}")

@task_postrun.connect
def task_postrun_handler(task_id=None, task=None, args=None, kwargs=None, retval=None, state=None, **kwds):
    print(f"Task finished: {task.name}({task_id}) state={state}")

@task_failure.connect
def task_failure_handler(task_id=None, exception=None, traceback=None, einfo=None, **kwds):
    print(f"Task failed: {task_id} exception={exception}")

# Explicitly import task modules to ensure registration
from .tasks import document_extraction, post_upload, test_tasks, delete_source
