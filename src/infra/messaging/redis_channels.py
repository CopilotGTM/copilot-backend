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


def channel_for_chat_job(organisation_id: UUID | str, job_id: UUID | str) -> str:
    return _CHAT_JOB_TEMPLATE.format(org_id=organisation_id, job_id=job_id)


def channel_for_knowledge_update_status(organisation_id: UUID | str) -> str:
    return _KNOWLEDGE_TEMPLATE.format(org_id=organisation_id)


def channel_for_attachment_update_status(organisation_id: UUID | str) -> str:
    return _ATTACHMENT_TEMPLATE.format(org_id=organisation_id)
