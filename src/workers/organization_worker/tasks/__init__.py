# Import all task modules to ensure they are registered
from . import organization, signup, test_tasks

__all__ = ["organization", "signup", "test_tasks"]
