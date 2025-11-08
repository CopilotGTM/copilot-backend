"""CRM entity adapters."""

from .company_adapter import sync_companies
from .contact_adapter import sync_contacts
from .deal_adapter import sync_deals
from .deal_task_adapter import sync_deal_tasks
from .owner_adapter import sync_owners
from .pipeline_adapter import sync_pipelines
from .user_adapter import sync_users

__all__ = [
    "sync_companies",
    "sync_contacts",
    "sync_deals",
    "sync_deal_tasks",
    "sync_owners",
    "sync_pipelines",
    "sync_users",
]
