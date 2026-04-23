"""Salesforce post-connect initial-sync chain builder."""

from __future__ import annotations

from uuid import UUID

from celery.canvas import Signature, chain
from src.workers.integrations_worker.client import task_signature


def build_salesforce_initial_sync_chain(
    organisation_id: UUID,
    integration_id: UUID,
) -> Signature:
    kwargs = {
        "organisation_id": str(organisation_id),
        "integration_id": str(integration_id),
    }

    success_chain = chain(
        task_signature("integrations.sync_salesforce_opportunity_schema", kwargs=kwargs),
        task_signature("integrations.sync_salesforce_pipelines", kwargs=kwargs),
        task_signature("integrations.mark_salesforce_initial_sync_succeeded", kwargs=kwargs),
    )
    failure_terminator = task_signature(
        "integrations.mark_salesforce_initial_sync_failed",
        kwargs=kwargs,
    )
    return success_chain.set(link_error=failure_terminator)
