# Import all task modules to ensure they are registered
from . import message_processing, post_upload, test_tasks

__all__ = ["message_processing", "post_upload", "test_tasks"]
