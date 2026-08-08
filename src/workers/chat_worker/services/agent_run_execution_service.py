"""Database-safe orchestration for one Celery-driven agent run.

The worker commits and releases the claim session before model/tool execution,
then opens a fresh session for the fenced outcome. This prevents long agent
runs from pinning tenant database connections.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.agents.context import AgentContext
from src.domain.models.master_tables import (
    AdminUserStatus,
    Organisation,
    OrganisationStatus,
    SupabaseUserStatus,
    User,
)
from src.domain.models.organisation_tables import (
    AgentRun,
    AgentStatus,
    CRMCompany,
    CRMContact,
    CRMDeal,
    KnowledgeIntegration,
    UserAgent,
)
from src.domain.models.organisation_tables.sync.email_models import Email
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.observability.metrics import record_agent_run_delivery
from src.infra.utils.logger import get_logger
from src.service.automation.producers.meeting import load_meeting_event_context
from src.service.automation.runs.claim_service import (
    AgentRunClaimService,
    InvalidClaimedAgentRunError,
)
from src.service.automation.runs.lifecycle_service import AgentRunLifecycleService
from src.service.automation.runs.runtime_executor import (
    AgentRunExecutor,
    RuntimeAuthorityError,
)
from src.service.automation.triggers.direct import crm_user_ids_for_principal
from src.service.automation.triggers.runtime import AgentRunRuntimeEnvelope
from src.workers.chat_worker.client import send_chat_task_async

logger = get_logger("agent_run_execution")

SessionScope = Callable[[UUID], AsyncIterator[AsyncSession]]
ContextLoader = Callable[[AgentRunRuntimeEnvelope], Awaitable[AgentContext]]
DeliveryPublisher = Callable[[UUID, UUID], Awaitable[None]]


async def _publish_delivery(organisation_id: UUID, delivery_id: UUID) -> None:
    """Publish a committed delivery identity to the provider-facing worker queue."""

    await send_chat_task_async(
        "chat.deliver_agent_run",
        kwargs={
            "organisation_id": str(organisation_id),
            "delivery_id": str(delivery_id),
        },
        queue="chat_messages",
        routing_key="chat_messages",
    )


class AgentRunExecutionService:
    """Claim, execute, and finalize one durable run without a long DB session."""

    def __init__(
        self,
        organisation_id: UUID,
        *,
        session_scope: SessionScope = acquire_org_async_session,
        claim_factory: Callable[[AsyncSession, UUID], Any] = AgentRunClaimService,
        lifecycle_factory: Callable[[AsyncSession, UUID], Any] = AgentRunLifecycleService,
        executor: AgentRunExecutor | None = None,
        context_loader: ContextLoader | None = None,
        delivery_publisher: DeliveryPublisher | None = None,
    ) -> None:
        self.organisation_id = organisation_id
        self.session_scope = session_scope
        self.claim_factory = claim_factory
        self.lifecycle_factory = lifecycle_factory
        self.executor = executor or AgentRunExecutor()
        self.context_loader = context_loader or self._load_context
        self.delivery_publisher = delivery_publisher or _publish_delivery

    async def execute(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        retry_allowed: bool,
    ) -> dict[str, Any]:
        """Execute one claimed run and publish only delivery rows committed with it.

        A failed delivery publication is recoverable independently, so this task
        must never release and rerun a model execution that already completed.
        """

        envelope, claim_status = await self._claim(run_id, worker_id)
        if envelope is None:
            return {"status": claim_status, "run_id": str(run_id)}

        try:
            context = await self.context_loader(envelope)
            result = await self.executor.execute(envelope, context=context)
        except RuntimeAuthorityError as exc:
            status = await self._mark_failed(
                envelope,
                error_code="runtime_authority_invalid",
                error_message=str(exc)[:1000],
                validation_failure=True,
                retry_allowed=retry_allowed,
            )
            return {"status": status, "run_id": str(run_id)}
        except ValueError as exc:
            status = await self._mark_failed(
                envelope,
                error_code="invalid_agent_output",
                error_message=str(exc)[:1000],
                validation_failure=True,
                retry_allowed=retry_allowed,
            )
            return {"status": status, "run_id": str(run_id)}
        except Exception:
            logger.exception(
                "Agent execution failed org=%s run=%s attempt=%d",
                self.organisation_id,
                run_id,
                envelope.attempt_number,
            )
            if retry_allowed:
                updated = await self._release_for_retry(envelope)
                return {
                    "status": "retry" if updated else "superseded",
                    "run_id": str(run_id),
                }
            status = await self._mark_failed(
                envelope,
                error_code="agent_execution_failed",
                error_message="Agent execution failed after retries",
                validation_failure=False,
                retry_allowed=False,
            )
            return {"status": status, "run_id": str(run_id)}

        async for session in self.session_scope(self.organisation_id):
            lifecycle = self.lifecycle_factory(session, self.organisation_id)
            try:
                completion = await lifecycle.mark_succeeded(
                    run_id,
                    attempt_number=envelope.attempt_number,
                    worker_id=envelope.worker_id,
                    output_text=result.output_text,
                    output_json=result.output_json,
                    model_name=result.model_name,
                    token_usage=result.token_usage,
                    cost=result.cost,
                    latency_ms=result.latency_ms,
                )
                await session.commit()
            except Exception as exc:
                released = await self._release_after_finalization_failure(
                    session,
                    lifecycle,
                    envelope,
                    exc,
                )
                if released:
                    return {
                        "status": "retry" if retry_allowed else "pending",
                        "run_id": str(run_id),
                    }
                return {"status": "superseded", "run_id": str(run_id)}
            updated = completion.updated
            # The run and delivery ledger are durable before the broker call. A
            # publication failure is handled by delivery recovery, not run retry.
            for delivery_id in completion.delivery_ids:
                record_agent_run_delivery("created")
                try:
                    await self.delivery_publisher(
                        self.organisation_id,
                        delivery_id,
                    )
                except Exception:
                    # The durable delivery row is recovered independently. The
                    # completed model run must never be released and executed again.
                    logger.exception(
                        "Agent run delivery publication failed org=%s run=%s",
                        self.organisation_id,
                        run_id,
                    )
            return {
                "status": "succeeded" if updated else "superseded",
                "run_id": str(run_id),
            }
        raise RuntimeError("organization session scope yielded no session")

    async def _claim(
        self,
        run_id: UUID,
        worker_id: str,
    ) -> tuple[AgentRunRuntimeEnvelope | None, str]:
        async for session in self.session_scope(self.organisation_id):
            claim = self.claim_factory(session, self.organisation_id)
            try:
                envelope = await claim.claim_run(run_id, worker_id=worker_id)
            except InvalidClaimedAgentRunError as exc:
                lifecycle = self.lifecycle_factory(session, self.organisation_id)
                await lifecycle.mark_failed(
                    exc.run_id,
                    attempt_number=exc.attempt_number,
                    worker_id=exc.worker_id,
                    error_code="invalid_agent_run",
                    error_message=str(exc)[:1000],
                    validation_failure=True,
                    latency_ms=0,
                )
                await session.commit()
                return None, "validation_failed"

            if envelope is None:
                return None, "skipped"

            # Claim ownership must be durable before slow model or tool calls.
            # Releasing this scope returns the tenant connection to the pool.
            await session.commit()
            return envelope, "claimed"
        raise RuntimeError("organization session scope yielded no session")

    async def _mark_failed(
        self,
        envelope: AgentRunRuntimeEnvelope,
        *,
        error_code: str,
        error_message: str,
        validation_failure: bool,
        retry_allowed: bool,
    ) -> str:
        async for session in self.session_scope(self.organisation_id):
            lifecycle = self.lifecycle_factory(session, self.organisation_id)
            try:
                updated = await lifecycle.mark_failed(
                    envelope.run_id,
                    attempt_number=envelope.attempt_number,
                    worker_id=envelope.worker_id,
                    error_code=error_code,
                    error_message=error_message,
                    validation_failure=validation_failure,
                    latency_ms=0,
                )
                await session.commit()
            except Exception as exc:
                released = await self._release_after_finalization_failure(
                    session,
                    lifecycle,
                    envelope,
                    exc,
                )
                if released:
                    return "retry" if retry_allowed else "pending"
                return "superseded"
            if not updated:
                return "superseded"
            return "validation_failed" if validation_failure else "failed"
        raise RuntimeError("organization session scope yielded no session")

    async def _release_after_finalization_failure(
        self,
        session: AsyncSession,
        lifecycle: Any,
        envelope: AgentRunRuntimeEnvelope,
        exc: Exception,
    ) -> bool:
        """Roll back a failed terminal append and release its exact claim fence."""

        logger.error(
            "Agent run terminal finalization failed org=%s run=%s attempt=%d",
            self.organisation_id,
            envelope.run_id,
            envelope.attempt_number,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        await session.rollback()
        released = await lifecycle.release_for_retry(
            envelope.run_id,
            attempt_number=envelope.attempt_number,
            worker_id=envelope.worker_id,
            error_code="terminal_finalization_failed",
            error_message=str(exc)[:1000],
        )
        await session.commit()
        return bool(released)

    async def _release_for_retry(self, envelope: AgentRunRuntimeEnvelope) -> bool:
        async for session in self.session_scope(self.organisation_id):
            lifecycle = self.lifecycle_factory(session, self.organisation_id)
            updated = await lifecycle.release_for_retry(
                envelope.run_id,
                attempt_number=envelope.attempt_number,
                worker_id=envelope.worker_id,
                error_code="agent_execution_retryable",
                error_message="Agent execution failed; retry scheduled",
            )
            await session.commit()
            return bool(updated)
        raise RuntimeError("organization session scope yielded no session")

    async def _load_context(self, envelope: AgentRunRuntimeEnvelope) -> AgentContext:
        """Revalidate mutable authority and release both DB sessions before agent work."""

        org_name: str | None = None
        user_email: str | None = None
        async for session in acquire_master_async_session():
            organisation = (
                await session.execute(
                    select(
                        col(Organisation.agent_name),
                        col(Organisation.organisation_name),
                    ).where(
                        col(Organisation.organisation_id) == envelope.organisation_id,
                        col(Organisation.is_active).is_(True),
                        col(Organisation.status) == OrganisationStatus.APPROVED,
                    )
                )
            ).one_or_none()
            if organisation is None:
                raise RuntimeAuthorityError("organisation is not active and approved")
            agent_name, organisation_name = organisation
            org_name = agent_name or organisation_name

            if envelope.principal_user_id is not None:
                user_email = (
                    await session.execute(
                        select(col(User.email)).where(
                            col(User.user_id) == envelope.principal_user_id,
                            col(User.organisation_id) == envelope.organisation_id,
                            col(User.status) == SupabaseUserStatus.ACTIVE,
                            col(User.admin_status) == AdminUserStatus.APPROVED,
                        )
                    )
                ).scalar_one_or_none()
                if user_email is None:
                    raise RuntimeAuthorityError("execution principal is not active and approved")

        async for session in self.session_scope(envelope.organisation_id):
            target_conditions = [
                col(UserAgent.organisation_id) == envelope.organisation_id,
                col(UserAgent.agent_id) == envelope.agent_id,
                col(UserAgent.status) == AgentStatus.ACTIVE,
            ]
            if envelope.principal_user_id is not None:
                target_conditions.append(col(UserAgent.owner_user_id) == envelope.principal_user_id)
            target_id = (
                await session.execute(select(col(UserAgent.agent_id)).where(*target_conditions))
            ).scalar_one_or_none()
            if target_id is None:
                raise RuntimeAuthorityError("agent target is not currently executable")
            await self._ensure_subject_authority(session, envelope)

        return AgentContext(
            organisation_id=envelope.organisation_id,
            deal_id=envelope.deal_id,
            subject_type=envelope.subject_type,
            subject_id=envelope.subject_id,
            org_name=org_name or "Our Organization",
            user_id=envelope.principal_user_id,
            user_email=user_email,
            channel="web",
            metadata={
                "source": "agent_trigger",
                "run_id": str(envelope.run_id),
                "trigger_id": str(envelope.trigger_id),
                "automation_agent_id": str(envelope.agent_id),
                "automation_agent_revision": envelope.agent_revision,
                "automation_inference_run_id": str(envelope.run_id),
                "automation_evidence_id": str(
                    envelope.input_snapshot.get("source", {}).get("source_event_id")
                    or envelope.event_id
                ),
                "automation_caused_by_run_id": str(envelope.run_id),
                "automation_correlation_id": envelope.correlation_id,
                "automation_causation_depth": envelope.causation_depth + 1,
            },
        )

    @staticmethod
    async def _ensure_subject_authority(
        session: AsyncSession,
        envelope: AgentRunRuntimeEnvelope,
    ) -> None:
        """Fail closed when a supported subject is gone or no longer in personal scope."""

        if envelope.subject_type is None:
            return
        try:
            subject_id = UUID(envelope.subject_id or "")
        except ValueError as exc:
            raise RuntimeAuthorityError("event subject identity is not a UUID") from exc

        principal_id = envelope.principal_user_id
        crm_user_ids = (
            crm_user_ids_for_principal(envelope.organisation_id, principal_id)
            if principal_id is not None
            else None
        )
        if envelope.subject_type == "deal":
            conditions = [
                col(CRMDeal.organisation_id) == envelope.organisation_id,
                col(CRMDeal.deal_id) == subject_id,
            ]
            if principal_id is not None:
                conditions.append(
                    or_(
                        col(CRMDeal.owner_id) == principal_id,
                        col(CRMDeal.created_by_user_id) == principal_id,
                        col(CRMDeal.owner_id).in_(crm_user_ids),
                        col(CRMDeal.created_by_user_id).in_(crm_user_ids),
                    )
                )
            statement = select(col(CRMDeal.deal_id)).where(*conditions)
        elif envelope.subject_type == "contact":
            conditions = [
                col(CRMContact.organisation_id) == envelope.organisation_id,
                col(CRMContact.contact_id) == subject_id,
            ]
            if principal_id is not None:
                conditions.append(
                    or_(
                        col(CRMContact.owner_id) == principal_id,
                        col(CRMContact.owner_id).in_(crm_user_ids),
                    )
                )
            statement = select(col(CRMContact.contact_id)).where(*conditions)
        elif envelope.subject_type == "account":
            conditions = [
                col(CRMCompany.organisation_id) == envelope.organisation_id,
                col(CRMCompany.company_id) == subject_id,
            ]
            if principal_id is not None:
                conditions.append(
                    or_(
                        col(CRMCompany.owner_id) == principal_id,
                        col(CRMCompany.owner_id).in_(crm_user_ids),
                    )
                )
            statement = select(col(CRMCompany.company_id)).where(*conditions)
        elif envelope.subject_type == "email":
            conditions = [
                col(Email.organisation_id) == envelope.organisation_id,
                col(Email.email_id) == subject_id,
                col(Email.is_deleted).is_(False),
            ]
            statement = select(col(Email.email_id)).join(
                KnowledgeIntegration,
                col(KnowledgeIntegration.integration_id) == col(Email.integration_id),
            )
            if principal_id is not None:
                conditions.append(col(KnowledgeIntegration.created_by_user_id) == principal_id)
            statement = statement.where(*conditions)
        elif envelope.subject_type == "meeting":
            meeting = (
                (
                    await session.execute(
                        select(Meeting).where(
                            col(Meeting.organisation_id) == envelope.organisation_id,
                            col(Meeting.meeting_id) == subject_id,
                            col(Meeting.is_deleted).is_(False),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if meeting is None:
                raise RuntimeAuthorityError("event subject is unavailable to the run principal")
            if principal_id is not None:
                # Event production and runtime must resolve the same owner.
                # Reusing the organiser-first resolver avoids mailbox sync state
                # overriding the internal organiser or raising on duplicate states.
                try:
                    context = await load_meeting_event_context(
                        session,
                        envelope.organisation_id,
                        meeting,
                    )
                except ValueError as exc:
                    raise RuntimeAuthorityError(
                        "event subject is unavailable to the run principal"
                    ) from exc
                if context.owner_id != principal_id:
                    raise RuntimeAuthorityError("event subject is unavailable to the run principal")
            return
        elif envelope.subject_type == "agent_run":
            conditions = [
                col(AgentRun.organisation_id) == envelope.organisation_id,
                col(AgentRun.run_id) == subject_id,
            ]
            if principal_id is not None:
                conditions.append(col(AgentRun.created_by_user_id) == principal_id)
            statement = select(col(AgentRun.run_id)).where(*conditions)
        else:
            # copilotgtm: New subject families stay disabled until their live
            # access query is added here; the subject registry is the upgrade path.
            raise RuntimeAuthorityError(
                f"subject type {envelope.subject_type!r} has no runtime access policy"
            )

        if (await session.execute(statement)).scalar_one_or_none() is None:
            raise RuntimeAuthorityError("event subject is unavailable to the run principal")
