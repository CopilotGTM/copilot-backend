"""User-facing read service for durable agent run history."""

from __future__ import annotations

from binascii import Error as BinasciiError
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.api.schemas.agent_run_models import (
    AgentRunDetail,
    AgentRunListResponse,
    AgentRunSummary,
)
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger
from src.service.automation.runs.read_access import AgentRunReadAccess

logger = get_logger("agent_run_read")


class AgentRunReadError(Exception):
    """Safe run-history failure returned by the user API."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ReadAccess(Protocol):
    """Persistence operations required by the read service."""

    async def list_runs(self, **kwargs) -> list[AgentRun]:
        """Return one owner-scoped keyset page."""

        ...

    async def get_run(self, run_id: UUID) -> AgentRun | None:
        """Return one owner-scoped run if it exists."""

        ...


class AgentRunReadService:
    """Return one authenticated user's compact and detailed run history."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        owner_user_id: UUID,
        *,
        access: ReadAccess | None = None,
    ) -> None:
        self.session = session
        self.access = access or AgentRunReadAccess(
            session,
            organisation_id,
            owner_user_id,
        )

    async def close(self) -> None:
        """Release the request-scoped tenant session."""

        try:
            await self.session.close()
        except Exception:
            logger.debug("AgentRunReadService close failed", exc_info=True)

    async def list_runs(
        self,
        *,
        agent_id: UUID | None,
        trigger_id: UUID | None,
        status: AgentRunStatus | None,
        cursor: str | None,
        limit: int,
    ) -> AgentRunListResponse:
        """Return a keyset page without loading mutable agent definitions."""

        if not 1 <= limit <= 100:
            raise AgentRunReadError("limit must be between 1 and 100")
        cursor_created_at: datetime | None = None
        cursor_run_id: UUID | None = None
        if cursor:
            try:
                cursor_created_at, cursor_run_id = decode_cursor(cursor)
            except (ValueError, KeyError, TypeError, UnicodeDecodeError, BinasciiError) as exc:
                raise AgentRunReadError("Invalid agent run cursor") from exc

        rows = await self.access.list_runs(
            agent_id=agent_id,
            trigger_id=trigger_id,
            status=status,
            cursor_created_at=cursor_created_at,
            cursor_run_id=cursor_run_id,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = (
            encode_cursor(page[-1].created_at, page[-1].run_id) if has_more and page else None
        )
        return AgentRunListResponse(
            items=[_summary(row) for row in page],
            next_cursor=next_cursor,
        )

    async def get_run(self, run_id: UUID) -> AgentRunDetail:
        """Return full metadata for one owned durable run."""

        row = await self.access.get_run(run_id)
        if row is None:
            raise AgentRunReadError("Agent run not found", status_code=404)
        return _detail(row)


def _summary(row: AgentRun) -> AgentRunSummary:
    preview = row.output_text[:300] if row.output_text else None
    return AgentRunSummary(
        run_id=row.run_id,
        agent_id=row.agent_id,
        trigger_id=row.trigger_id,
        agent_revision=row.agent_revision,
        status=row.status,
        event_type=row.event_type,
        attempt_count=row.attempt_count,
        model_name=row.model_name,
        latency_ms=row.latency_ms,
        error_code=row.error_code,
        error_message=row.error_message,
        output_preview=preview,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _detail(row: AgentRun) -> AgentRunDetail:
    return AgentRunDetail(
        run_id=row.run_id,
        agent_id=row.agent_id,
        trigger_id=row.trigger_id,
        agent_revision=row.agent_revision,
        status=row.status,
        source_domain=row.source_domain,
        event_type=row.event_type,
        source_event_id=row.source_event_id,
        scheduled_for=row.scheduled_for,
        attempt_count=row.attempt_count,
        claimed_at=row.claimed_at,
        claimed_by=row.claimed_by,
        model_name=row.model_name,
        token_usage=row.token_usage,
        cost=row.cost,
        latency_ms=row.latency_ms,
        error_code=row.error_code,
        error_message=row.error_message,
        output_text=row.output_text,
        output_json=row.output_json,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        trigger_snapshot=dict(row.trigger_snapshot),
        agent_snapshot=dict(row.agent_snapshot),
        input_snapshot=dict(row.input_snapshot),
    )
