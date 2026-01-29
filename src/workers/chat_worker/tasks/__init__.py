# Import all task modules to ensure they are registered
from . import bot_message_processing, message_processing, post_upload, test_tasks

__all__ = ["bot_message_processing", "message_processing", "post_upload", "test_tasks"]
