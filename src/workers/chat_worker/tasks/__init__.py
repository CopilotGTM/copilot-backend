# Import all task modules to ensure they are registered
from . import post_upload, test_tasks

__all__ = ["post_upload", "test_tasks"]
