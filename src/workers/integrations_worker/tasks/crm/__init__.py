"""CRM schema and record sync Celery tasks for integrations_worker."""

from src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_all_orgs_task import (
    sync_hubspot_deal_schema_all_orgs_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot_deal_schema_task import (
    sync_hubspot_deal_schema_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot_initial_sync_failed_task import (
    mark_hubspot_initial_sync_failed_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot_initial_sync_succeeded_task import (
    mark_hubspot_initial_sync_succeeded_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot_pipeline_record_sync_task import (
    sync_hubspot_pipelines_task,
)

__all__ = [
    "sync_hubspot_deal_schema_all_orgs_task",
    "sync_hubspot_deal_schema_task",
    "sync_hubspot_pipelines_task",
    "mark_hubspot_initial_sync_succeeded_task",
    "mark_hubspot_initial_sync_failed_task",
]
