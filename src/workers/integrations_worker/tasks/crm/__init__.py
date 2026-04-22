"""CRM schema and record sync Celery tasks for integrations_worker."""

from src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_all_orgs_task import (
    sync_hubspot_deal_schema_all_orgs_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task import (
    sync_hubspot_deal_schema_task,
)

__all__ = [
    "sync_hubspot_deal_schema_all_orgs_task",
    "sync_hubspot_deal_schema_task",
]
