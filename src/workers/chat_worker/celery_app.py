import os

# Set minimal DB pool settings for worker process
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"

from dotenv import load_dotenv
from src.workers.base.celery_base import create_celery_app
from src.workers.chat_worker.config import ChatWorkerConfig
from celery.signals import task_received, task_prerun, task_postrun, task_failure

# Load environment variables first
load_dotenv(override=True)

# Initialize the configuration for the Chat Worker
config = ChatWorkerConfig()

# Define the tasks module for auto-discovery
tasks_module = "src.workers.chat_worker.tasks"

# Create the Celery app for the Chat Worker
app = create_celery_app("chat_worker", config, tasks_module)

# Register DB pool monitor for Celery workers (signals-based)
try:
    import scripts.monitoring.celery_integration
except Exception:
    pass

# Add debug configuration
app.conf.update(
    worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
    worker_task_log_format="[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s",
)


# Add signal handlers for debugging
@task_received.connect
def task_received_handler(sender=None, headers=None, body=None, **kwargs):
    print(f"Task received: {headers}")


@task_prerun.connect
def task_prerun_handler(task_id=None, task=None, args=None, kwargs=None, **kwds):
    print(f"Task starting: {task.name}({task_id}) with args={args} kwargs={kwargs}")


@task_postrun.connect
def task_postrun_handler(
    task_id=None, task=None, args=None, kwargs=None, retval=None, state=None, **kwds
):
    print(f"Task finished: {task.name}({task_id}) state={state}")


@task_failure.connect
def task_failure_handler(
    task_id=None, exception=None, traceback=None, einfo=None, **kwds
):
    print(f"Task failed: {task_id} exception={exception}")


# Explicitly import task modules to ensure registration
from .tasks import post_upload, test_tasks, message_processing
