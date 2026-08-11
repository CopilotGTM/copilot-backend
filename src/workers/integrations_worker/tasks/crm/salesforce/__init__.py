"""Salesforce CRM task modules for integrations_worker."""

from src.workers.integrations_worker.tasks.crm.salesforce import salesforce_cdc_subscriber_task
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_cdc_repair_tasks import (
    mark_salesforce_cdc_repair_failed_task,
    mark_salesforce_cdc_repair_succeeded_task,
)
from src.workers.integrations_worker.tasks.crm.salesforce.salesforce_cdc_subscriber_task import (
    run_salesforce_cdc_subscriber_task,
)

__all__ = [
    "run_salesforce_cdc_subscriber_task",
    "salesforce_cdc_subscriber_task",
    "mark_salesforce_cdc_repair_succeeded_task",
    "mark_salesforce_cdc_repair_failed_task",
]
