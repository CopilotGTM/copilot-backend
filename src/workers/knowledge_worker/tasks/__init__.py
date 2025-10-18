# Import all task modules to ensure they are registered
from . import delete_source, document_extraction, post_upload, test_tasks

__all__ = ["document_extraction", "post_upload", "test_tasks", "delete_source"]
