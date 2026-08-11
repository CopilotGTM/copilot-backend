"""Build the one-at-a-time Salesforce aggregate CDC replay repair chain."""

from __future__ import annotations

from uuid import UUID

from celery.canvas import Signature, chain
from src.workers.integrations_worker.client import task_signature


def build_salesforce_cdc_repair_chain(
    organisation_id: UUID,
    integration_id: UUID,
) -> Signature:
    """Run all bounded current-state syncs serially before reopening aggregate replay."""
    kwargs = {
        "organisation_id": str(organisation_id),
        "integration_id": str(integration_id),
    }
    repair = chain(
        task_signature("integrations.sync_salesforce_opportunity_schema", kwargs=kwargs),
        task_signature("integrations.sync_salesforce_pipelines", kwargs=kwargs),
        task_signature("integrations.sync_salesforce_users", kwargs=kwargs),
        task_signature("integrations.sync_salesforce_companies", kwargs=kwargs),
        task_signature("integrations.sync_salesforce_contacts", kwargs=kwargs),
        task_signature("integrations.sync_salesforce_deals", kwargs=kwargs),
        task_signature("integrations.sync_salesforce_tasks", kwargs=kwargs),
        task_signature("integrations.sync_salesforce_notes", kwargs=kwargs),
        task_signature("integrations.mark_salesforce_cdc_repair_succeeded", kwargs=kwargs),
    )
    failure = task_signature(
        "integrations.mark_salesforce_cdc_repair_failed",
        kwargs=kwargs,
    )
    return repair.set(link_error=failure)


async def publish_salesforce_cdc_repair_chain(
    organisation_id: UUID,
    integration_id: UUID,
) -> None:
    """Publish the already-fenced serial repair to the existing priority queue."""
    build_salesforce_cdc_repair_chain(organisation_id, integration_id).apply_async(
        queue="integrations_priority",
        routing_key="integrations_priority",
    )
