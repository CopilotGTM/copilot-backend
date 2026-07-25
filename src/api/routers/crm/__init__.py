"""CRM routers package for authenticated and org-scoped API surfaces.

This module centralizes router-level imports and a single `__all__` export list so
route groups can be mounted consistently from the shared application factory.
"""

from .accounts_handler import accounts_router
from .contacts_handler import contacts_router
from .crm_meetings_handler import crm_meetings_router
from .custom_fields_handler import custom_fields_router
from .deal_assets_handler import deal_assets_router
from .deal_memory_handler import deal_memory_router
from .deal_notes_handler import deal_notes_router
from .deal_participants_handler import deal_participants_router
from .deals_handler import deals_router
from .emails_handler import emails_router
from .imports_handler import imports_router
from .leads import (
    lead_forms_router,
    lead_ingestion_keys_router,
    leads_router,
    public_lead_forms_router,
)
from .meetings_handler import meetings_router
from .pipelines_handler import pipelines_router
from .risks_blockers_handler import risks_blockers_router
from .settings_handler import settings_router
from .stakeholder_insights_handler import stakeholder_insights_router
from .stakeholders_handler import stakeholders_router
from .tasks_handler import tasks_router
from .users_handler import crm_users_router

__all__ = [
    "accounts_router",
    "contacts_router",
    "crm_meetings_router",
    "deal_assets_router",
    "deal_memory_router",
    "deal_notes_router",
    "deal_participants_router",
    "custom_fields_router",
    "deals_router",
    "emails_router",
    "imports_router",
    "lead_forms_router",
    "lead_ingestion_keys_router",
    "public_lead_forms_router",
    "leads_router",
    "meetings_router",
    "pipelines_router",
    "risks_blockers_router",
    "settings_router",
    "stakeholder_insights_router",
    "stakeholders_router",
    "tasks_router",
    "crm_users_router",
]
