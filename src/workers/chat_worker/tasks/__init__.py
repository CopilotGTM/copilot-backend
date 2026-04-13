# Import all task modules to ensure they are registered
from . import (
    bot_message_processing,
    message_processing,
    post_upload,
    reminder_tasks,
    stale_job_cleanup_tasks,
    test_tasks,
    voice_realtime,
)

__all__ = [
    "bot_message_processing",
    "message_processing",
    "post_upload",
    "reminder_tasks",
    "stale_job_cleanup_tasks",
    "test_tasks",
    "voice_realtime",
]
