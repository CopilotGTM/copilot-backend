# Import all task modules to ensure they are registered
from . import (
    delete_source,
    document_extraction,
    post_upload,
    stale_connection_cleanup_tasks,
    stale_job_cleanup_tasks,
    test_tasks,
)

__all__ = [
    "document_extraction",
    "post_upload",
    "test_tasks",
    "delete_source",
    "stale_job_cleanup_tasks",
    "stale_connection_cleanup_tasks",
]
