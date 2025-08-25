# Import all task modules to ensure they are registered
from . import document_extraction
from . import post_upload
from . import test_tasks

__all__ = ['document_extraction', 'post_upload', 'test_tasks']