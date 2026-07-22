"""Transaction-local registration of source events for trigger activation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import AutomationEventDispatch
from src.service.automation.errors import AutomationServiceError
from src.service.automation.events.access import AutomationEventDispatchAccess
from src.service.automation.events.delivery import stage_automation_event_delivery
from src.service.automation.triggers.crm_events import CRM_AGENT_EVENT_SCHEMAS


class DispatchAccess(Protocol):
    """Persistence operations required by the enqueue service."""

    async def insert_if_absent(self, dispatch: AutomationEventDispatch) -> bool:
        """Insert a dispatch unless the same source occurrence already exists."""

        ...

    async def get_by_source(
        self, *, source_domain: str, source_event_id: str, event_type: str
    ) -> AutomationEventDispatch | None:
        """Load the existing row created by an earlier producer delivery."""

        ...


class AutomationEventDispatchService:
    """Append supported event identities to the durable activation outbox."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        access: DispatchAccess | None = None,
        delivery_stager: Callable[
            [AsyncSession, AutomationEventDispatch], None
        ] = stage_automation_event_delivery,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.access = access or AutomationEventDispatchAccess(session, organisation_id)
        self.delivery_stager = delivery_stager

    async def enqueue(
        self, *, source_domain: str, source_event_id: str, event_type: str
    ) -> AutomationEventDispatch:
        """Persist one supported semantic event without committing or publishing."""

        if source_domain != "crm" or event_type not in CRM_AGENT_EVENT_SCHEMAS:
            raise AutomationServiceError("Unsupported automation event")
        if not source_event_id:
            raise AutomationServiceError("Automation source_event_id is required")

        dispatch = AutomationEventDispatch(
            organisation_id=self.organisation_id,
            source_domain=source_domain,
            source_event_id=source_event_id,
            event_type=event_type,
        )
        if not await self.access.insert_if_absent(dispatch):
            existing = await self.access.get_by_source(
                source_domain=source_domain,
                source_event_id=source_event_id,
                event_type=event_type,
            )
            if existing is None:
                raise AutomationServiceError(
                    "Automation dispatch replay could not resolve its existing row"
                )
            dispatch = existing

        self.delivery_stager(self.session, dispatch)
        return dispatch
