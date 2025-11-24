"""CRM routers package."""

from .deals_handler import deals_router
from .meetings_handler import meetings_router
from .stakeholders_handler import stakeholders_router
from .tasks_handler import tasks_router

__all__ = ["deals_router", "tasks_router", "stakeholders_router", "meetings_router"]
