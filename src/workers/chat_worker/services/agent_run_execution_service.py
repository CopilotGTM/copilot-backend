"""Database-safe orchestration for one Celery-driven agent run.

The worker commits and releases the claim session before model/tool execution,
then opens a fresh session for the fenced outcome. This prevents long agent
runs from pinning tenant database connections.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.agents.context import AgentContext
from src.domain.models.master_tables import User
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.utils.logger import get_logger
from src.service.automation.runs.claim_service import (
    AgentRunClaimService,
    InvalidClaimedAgentRunError,
)
from src.service.automation.runs.lifecycle_service import AgentRunLifecycleService
from src.service.automation.runs.runtime_executor import AgentRunExecutor
from src.service.automation.triggers.runtime import AgentRunRuntimeEnvelope
from src.workers.base.worker_utils import get_organisation_name

logger = get_logger("agent_run_execution")

SessionScope = Callable[[UUID], AsyncIterator[AsyncSession]]
ContextLoader = Callable[[AgentRunRuntimeEnvelope], Awaitable[AgentContext]]


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
    ) -> None:
        self.organisation_id = organisation_id
        self.session_scope = session_scope
        self.claim_factory = claim_factory
        self.lifecycle_factory = lifecycle_factory
        self.executor = executor or AgentRunExecutor()
        self.context_loader = context_loader or self._load_context

    async def execute(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        retry_allowed: bool,
    ) -> dict[str, Any]:
        """Execute one run and return whether Celery should retry its delivery."""

        envelope, claim_status = await self._claim(run_id, worker_id)
        if envelope is None:
            return {"status": claim_status, "run_id": str(run_id)}

        try:
            context = await self.context_loader(envelope)
            result = await self.executor.execute(envelope, context=context)
        except ValueError as exc:
            updated = await self._mark_failed(
                envelope,
                error_code="invalid_agent_output",
                error_message=str(exc)[:1000],
                validation_failure=True,
            )
            return {
                "status": "validation_failed" if updated else "superseded",
                "run_id": str(run_id),
            }
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
            updated = await self._mark_failed(
                envelope,
                error_code="agent_execution_failed",
                error_message="Agent execution failed after retries",
                validation_failure=False,
            )
            return {
                "status": "failed" if updated else "superseded",
                "run_id": str(run_id),
            }

        async for session in self.session_scope(self.organisation_id):
            lifecycle = self.lifecycle_factory(session, self.organisation_id)
            updated = await lifecycle.mark_succeeded(
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
    ) -> bool:
        async for session in self.session_scope(self.organisation_id):
            lifecycle = self.lifecycle_factory(session, self.organisation_id)
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
            return bool(updated)
        raise RuntimeError("organization session scope yielded no session")

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
        """Resolve display metadata, then release master DB before agent work."""

        org_name = "Our Organization"
        user_email: str | None = None
        # The master session is deliberately released before the LLM call;
        # selected tools acquire their own short-lived tenant sessions on demand.
        async for session in acquire_master_async_session():
            org_name = await get_organisation_name(session, envelope.organisation_id) or org_name
            user_email = (
                await session.execute(
                    select(col(User.email)).where(col(User.user_id) == envelope.owner_user_id)
                )
            ).scalar_one_or_none()

        return AgentContext(
            organisation_id=envelope.organisation_id,
            deal_id=envelope.deal_id,
            org_name=org_name,
            user_id=envelope.owner_user_id,
            user_email=user_email,
            channel="web",
            metadata={
                "source": "agent_trigger",
                "run_id": str(envelope.run_id),
                "trigger_id": str(envelope.trigger_id),
            },
        )
