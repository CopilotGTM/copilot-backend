"""Channel / stream naming helpers decoupled from specific transport (pubsub vs streams).

This allows services using Redis Streams to avoid importing the pub/sub implementation.
Existing code can migrate to these helpers; legacy functions remain in `redis_pubsub`.
"""

from __future__ import annotations

from uuid import UUID

# Base templates
_CHAT_JOB_TEMPLATE = "chat:{org_id}:job:{job_id}"
_KNOWLEDGE_TEMPLATE = "kb:{org_id}:sources"
_ATTACHMENT_TEMPLATE = "att:{org_id}:attachments"
_DEAL_JOB_TEMPLATE = "deal:{org_id}:job:{job_id}"
_DEAL_ASSET_TEMPLATE = "deal_asset:{org_id}:assets"
_ORG_JOB_TEMPLATE = "org:job:{job_id}"


def channel_for_chat_job(organisation_id: UUID | str, job_id: UUID | str) -> str:
    return _CHAT_JOB_TEMPLATE.format(org_id=organisation_id, job_id=job_id)


def channel_for_knowledge_update_status(organisation_id: UUID | str) -> str:
    return _KNOWLEDGE_TEMPLATE.format(org_id=organisation_id)


def channel_for_attachment_update_status(organisation_id: UUID | str) -> str:
    return _ATTACHMENT_TEMPLATE.format(org_id=organisation_id)


def channel_for_deal_asset_status(organisation_id: UUID | str) -> str:
    return _DEAL_ASSET_TEMPLATE.format(org_id=organisation_id)


def channel_for_deal_job(organisation_id: UUID | str, job_id: UUID | str) -> str:
    """
    Generic channel for any deal job type (briefing, summary, stakeholder analysis, etc.).

    The job_id is unique, so we don't need job_type in the channel name.
    """
    return _DEAL_JOB_TEMPLATE.format(org_id=organisation_id, job_id=job_id)


def channel_for_org_job(job_id: UUID | str) -> str:
    """Channel for organisation job status updates (create, update, delete, migrate).

    Organisation jobs are master-level (not tenant-scoped), so channel only includes job_id.
    """
    return _ORG_JOB_TEMPLATE.format(job_id=job_id)
