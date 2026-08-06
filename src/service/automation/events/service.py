"""Transaction-local ingestion of canonical automation events."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import AutomationEventDispatch, AutomationRouteMode
from src.service.automation.errors import AutomationServiceError
from src.service.automation.events.access import AutomationEventDispatchAccess
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.events.delivery import stage_automation_event_delivery
from src.service.automation.triggers.crm_schema import AutomationCRMRegistryService
from src.service.automation.triggers.registry import AutomationRegistry


class DispatchAccess(Protocol):
    """Persistence operations required by canonical event ingestion."""

    async def insert_if_absent(self, dispatch: AutomationEventDispatch) -> bool:
        """Apply the insert if absent contract for this organisation-scoped module."""
        ...

    async def get_by_idempotency_key(self, key: str) -> AutomationEventDispatch | None:
        """Load by idempotency key within the organisation boundary."""
        ...


class AutomationEventDispatchService:
    """Validate and append one producer envelope without owning the transaction."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        access: DispatchAccess | None = None,
        delivery_stager: Callable[
            [AsyncSession, AutomationEventDispatch], None
        ] = stage_automation_event_delivery,
        registry: AutomationRegistry | None = None,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.access = access or AutomationEventDispatchAccess(session, organisation_id)
        self.delivery_stager = delivery_stager
        self.registry = registry
        self.registry_service = AutomationCRMRegistryService(session, organisation_id)

    async def enqueue(
        self,
        envelope: AutomationEventEnvelope,
    ) -> AutomationEventDispatch:
        """Persist one validated event and repair post-commit delivery on replay."""

        if envelope.organisation_id != self.organisation_id:
            raise AutomationServiceError("Automation event organisation does not match session")
        try:
            registry = self.registry or (await self.registry_service.snapshot()).registry
            definition = registry.event(envelope.event_key)
            if not definition.producer_available:
                raise ValueError("automation event producer is unavailable")
            definition.validate_envelope(envelope)
        except ValueError as exc:
            raise AutomationServiceError(str(exc)) from exc

        serialized = envelope.model_dump(mode="json")
        dispatch = AutomationEventDispatch(
            event_id=envelope.event_id,
            organisation_id=self.organisation_id,
            event_key=envelope.event_key,
            route_mode=AutomationRouteMode(envelope.route.mode.value),
            direct_trigger_id=envelope.route.trigger_id,
            idempotency_key=envelope.idempotency_key,
            envelope=serialized,
        )
        if not await self.access.insert_if_absent(dispatch):
            existing = await self.access.get_by_idempotency_key(envelope.idempotency_key)
            if existing is None:
                raise AutomationServiceError(
                    "Automation event replay could not resolve its existing row",
                    status_code=409,
                )
            if existing.envelope != serialized:
                raise AutomationServiceError(
                    "Automation event idempotency key was reused for a different envelope",
                    status_code=409,
                )
            dispatch = existing

        self.delivery_stager(self.session, dispatch)
        return dispatch
