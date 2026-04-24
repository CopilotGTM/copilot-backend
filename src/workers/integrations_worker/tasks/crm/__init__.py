"""CRM schema and record sync Celery tasks for integrations_worker."""

from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_company_record_sync_task import (
    sync_hubspot_companies_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_contact_record_sync_task import (
    sync_hubspot_contacts_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_deal_record_sync_task import (
    sync_hubspot_deals_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_deal_schema_all_orgs_task import (
    sync_hubspot_deal_schema_all_orgs_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_deal_schema_task import (
    sync_hubspot_deal_schema_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_initial_sync_failed_task import (
    mark_hubspot_initial_sync_failed_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_initial_sync_succeeded_task import (
    mark_hubspot_initial_sync_succeeded_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_note_record_sync_task import (
    sync_hubspot_notes_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_pipeline_record_sync_task import (
    sync_hubspot_pipelines_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_task_record_sync_task import (
    sync_hubspot_tasks_task,
)
from src.workers.integrations_worker.tasks.crm.hubspot.hubspot_user_record_sync_task import (
    sync_hubspot_users_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_company_record_sync_task import (
    sync_salesforce_companies_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_contact_record_sync_task import (
    sync_salesforce_contacts_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_deal_record_sync_task import (
    sync_salesforce_deals_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_initial_sync_failed_task import (
    mark_salesforce_initial_sync_failed_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_initial_sync_succeeded_task import (
    mark_salesforce_initial_sync_succeeded_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_note_record_sync_task import (
    sync_salesforce_notes_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_opportunity_schema_all_orgs_task import (
    sync_salesforce_opportunity_schema_all_orgs_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_opportunity_schema_task import (
    sync_salesforce_opportunity_schema_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_pipeline_record_sync_task import (
    sync_salesforce_pipelines_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_task_record_sync_task import (
    sync_salesforce_tasks_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_user_record_sync_task import (
    sync_salesforce_users_task,
)

__all__ = [
    "sync_hubspot_deal_schema_all_orgs_task",
    "sync_hubspot_deal_schema_task",
    "sync_hubspot_pipelines_task",
    "sync_hubspot_users_task",
    "sync_hubspot_companies_task",
    "sync_hubspot_contacts_task",
    "sync_hubspot_deals_task",
    "sync_hubspot_tasks_task",
    "sync_hubspot_notes_task",
    "mark_hubspot_initial_sync_succeeded_task",
    "mark_hubspot_initial_sync_failed_task",
    "sync_salesforce_opportunity_schema_task",
    "sync_salesforce_opportunity_schema_all_orgs_task",
    "sync_salesforce_pipelines_task",
    "sync_salesforce_users_task",
    "sync_salesforce_companies_task",
    "sync_salesforce_contacts_task",
    "sync_salesforce_deals_task",
    "sync_salesforce_tasks_task",
    "sync_salesforce_notes_task",
    "mark_salesforce_initial_sync_succeeded_task",
    "mark_salesforce_initial_sync_failed_task",
]
