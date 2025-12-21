"""CRM routers package."""

from .deal_timeline_handler import deal_timeline_router
from .deals_handler import deals_router
from .meetings_handler import meetings_router
from .risks_blockers_handler import risks_blockers_router
from .stakeholders_handler import stakeholders_router
from .tasks_handler import tasks_router

__all__ = [
    "deals_router",
    "tasks_router",
    "stakeholders_handler",
    "meetings_router",
    "deal_timeline_router",
    "risks_blockers_router",
]
