"""Commit durable trigger activation before publishing agent execution work.

Called by the chat worker for one organization-scoped dispatch. Database work
finishes before Redis publication so slow or failed broker calls never pin a
tenant connection; existing AgentRun recovery closes the post-commit gap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.automation.events.access import AutomationEventDispatchAccess
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.events.delivery import (
    DEFAULT_PUBLISH_RETRY_DELAYS,
    publish_task_with_backoff,
)
from src.service.automation.triggers.activation import AutomationTriggerActivationService
from src.workers.chat_worker.client import send_chat_task_async

logger = get_logger("automation_event_activation")

SessionScope = Callable[[UUID], AsyncIterator[AsyncSession]]
Publisher = Callable[..., Awaitable[Any]]


class AutomationEventActivationExecutionService:
    """Activate one outbox row and publish only its newly created agent runs."""

    def __init__(
        self,
        organisation_id: UUID,
        *,
        session_scope: SessionScope = acquire_org_async_session,
        access_factory: Callable[[AsyncSession, UUID], Any] = AutomationEventDispatchAccess,
        activation_factory: Callable[[AsyncSession, UUID], Any] = (
            AutomationTriggerActivationService
        ),
        publisher: Publisher = send_chat_task_async,
        publish_retry_delays: tuple[float, ...] = DEFAULT_PUBLISH_RETRY_DELAYS,
    ) -> None:
        self.organisation_id = organisation_id
        self.session_scope = session_scope
        self.access_factory = access_factory
        self.activation_factory = activation_factory
        self.publisher = publisher
        self.publish_retry_delays = publish_retry_delays

    async def activate(self, event_id: UUID, *, retry_allowed: bool) -> dict[str, Any]:
        """Process one event, returning whether Celery should retry failures."""

        created_run_ids: tuple[UUID, ...] = ()
        try:
            async for session in self.session_scope(self.organisation_id):
                access = self.access_factory(session, self.organisation_id)
                dispatch = await access.get(event_id)
                if dispatch is None:
                    return {"status": "not_found", "event_id": str(event_id)}
                if dispatch.processed_at is not None:
                    return {"status": "already_processed", "event_id": str(event_id)}
                if dispatch.failed_at is not None:
                    return {"status": "already_failed", "event_id": str(event_id)}

                try:
                    envelope = AutomationEventEnvelope.model_validate(dispatch.envelope)
                    if envelope.event_id != dispatch.event_id:
                        raise ValueError("Persisted envelope event_id does not match its row")
                    activation = await self.activation_factory(
                        session,
                        self.organisation_id,
                    ).activate(
                        envelope,
                    )
                except Exception:
                    # Clear any partial SQL state before failure bookkeeping opens a
                    # fresh transaction; PostgreSQL rejects writes on an aborted one.
                    await session.rollback()
                    raise
                created_run_ids = activation.created_run_ids
                if not await access.mark_processed(
                    event_id,
                    candidate_count=activation.candidate_count,
                    matched_count=activation.matched_count,
                    activation_outcomes=[
                        item.as_dict(
                            correlation_id=(activation.correlation_id or envelope.correlation_id),
                            latency_ms=activation.latency_ms,
                        )
                        for item in activation.outcomes
                    ],
                ):
                    await session.rollback()
                    return {"status": "superseded", "event_id": str(event_id)}

                # AgentRun rows and terminal dispatch state commit together. Publishing
                # before this point could let a fast worker observe uncommitted work.
                await session.commit()
                break
            else:
                raise RuntimeError("organization session scope yielded no session")
        except Exception:
            logger.exception(
                "Automation event activation failed org=%s event=%s",
                self.organisation_id,
                event_id,
            )
            await self._record_failure(
                event_id,
                terminal=not retry_allowed,
            )
            return {
                "status": "retry" if retry_allowed else "failed",
                "event_id": str(event_id),
            }

        published = 0
        publish_failures = 0
        for run_id in created_run_ids:
            delivered = await publish_task_with_backoff(
                "chat.execute_agent_run",
                kwargs={
                    "organisation_id": str(self.organisation_id),
                    "run_id": str(run_id),
                },
                queue="chat_messages",
                publisher=self.publisher,
                retry_delays=self.publish_retry_delays,
            )
            if delivered:
                published += 1
            else:
                publish_failures += 1

        return {
            "status": "processed",
            "created_runs": len(created_run_ids),
            "published_runs": published,
            "publish_failures": publish_failures,
        }

    async def _record_failure(self, event_id: UUID, *, terminal: bool) -> None:
        """Persist retry state in a fresh transaction after activation rollback."""

        async for session in self.session_scope(self.organisation_id):
            access = self.access_factory(session, self.organisation_id)
            await access.record_failure(
                event_id,
                error_code="activation_failed",
                error="Automation event activation failed",
                terminal=terminal,
            )
            await session.commit()
            return
        raise RuntimeError("organization session scope yielded no session")
