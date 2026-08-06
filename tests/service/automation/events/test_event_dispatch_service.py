"""Regression tests for test event dispatch; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables import AutomationEventDispatch
from src.service.automation.errors import AutomationServiceError
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.events.service import AutomationEventDispatchService
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY


def envelope(*, organisation_id=None, event_id=None, idempotency_key=None):
    """Build the envelope fixture used by the surrounding regression cases."""
    organisation_id = organisation_id or uuid4()
    event_id = event_id or uuid4()
    subject_id = uuid4()
    return AutomationEventEnvelope.model_validate(
        {
            "event_id": str(event_id),
            "organisation_id": str(organisation_id),
            "event_key": "crm.contact.created",
            "route": {"mode": "match"},
            "occurred_at": datetime(2026, 8, 5, tzinfo=UTC),
            "source": {
                "domain": "crm",
                "system": "internal",
                "operation": "create",
                "mode": "interactive",
                "source_event_id": str(event_id),
            },
            "actor": {"type": "user", "id": str(uuid4())},
            "subject": {"type": "contact", "id": str(subject_id)},
            "record": {"id": str(subject_id)},
            "idempotency_key": idempotency_key or f"crm.contact.created:{event_id}",
        }
    )


class FakeAccess:
    """Represent the Fake Access test double used by the surrounding regression cases."""

    def __init__(self, *, inserted: bool = True) -> None:
        self.inserted = inserted
        self.rows: dict[str, AutomationEventDispatch] = {}

    async def insert_if_absent(self, dispatch: AutomationEventDispatch) -> bool:
        """Expose the insert if absent operation expected by the Fake Access test double."""
        if self.inserted:
            self.rows[dispatch.idempotency_key] = dispatch
        return self.inserted

    async def get_by_idempotency_key(self, key: str) -> AutomationEventDispatch | None:
        """Expose the get by idempotency key operation expected by the Fake Access test double."""
        return self.rows.get(key)


@pytest.mark.asyncio
async def test_enqueue_validates_persists_and_stages_the_canonical_envelope() -> None:
    """Verify the contract that enqueue validates persists and stages the canonical envelope."""
    event = envelope()
    access = FakeAccess()
    staged: list[AutomationEventDispatch] = []
    service = AutomationEventDispatchService(
        object(),  # type: ignore[arg-type]
        event.organisation_id,
        access=access,
        delivery_stager=lambda _session, dispatch: staged.append(dispatch),
        registry=AUTOMATION_REGISTRY,
    )

    dispatch = await service.enqueue(event)

    assert dispatch.event_id == event.event_id
    assert dispatch.event_key == event.event_key
    assert dispatch.route_mode == "match"
    assert dispatch.direct_trigger_id is None
    assert dispatch.idempotency_key == event.idempotency_key
    assert dispatch.envelope == event.model_dump(mode="json")
    assert staged == [dispatch]


@pytest.mark.asyncio
async def test_identical_replay_reuses_and_restages_the_existing_dispatch() -> None:
    """Verify the contract that identical replay reuses and restages the existing dispatch."""
    event = envelope()
    existing = AutomationEventDispatch(
        event_id=event.event_id,
        organisation_id=event.organisation_id,
        event_key=event.event_key,
        route_mode="match",
        idempotency_key=event.idempotency_key,
        envelope=event.model_dump(mode="json"),
    )
    access = FakeAccess(inserted=False)
    access.rows[event.idempotency_key] = existing
    staged: list[AutomationEventDispatch] = []
    service = AutomationEventDispatchService(
        object(),  # type: ignore[arg-type]
        event.organisation_id,
        access=access,
        delivery_stager=lambda _session, dispatch: staged.append(dispatch),
        registry=AUTOMATION_REGISTRY,
    )

    replay = await service.enqueue(event)

    assert replay is existing
    assert staged == [existing]


@pytest.mark.asyncio
async def test_reused_idempotency_key_with_different_envelope_is_a_conflict() -> None:
    """Verify the contract that reused idempotency key with different envelope is a conflict."""
    first = envelope(idempotency_key="same-key")
    second = envelope(
        organisation_id=first.organisation_id,
        idempotency_key="same-key",
    )
    existing = AutomationEventDispatch(
        event_id=first.event_id,
        organisation_id=first.organisation_id,
        event_key=first.event_key,
        route_mode="match",
        idempotency_key=first.idempotency_key,
        envelope=first.model_dump(mode="json"),
    )
    access = FakeAccess(inserted=False)
    access.rows[first.idempotency_key] = existing
    service = AutomationEventDispatchService(
        object(),  # type: ignore[arg-type]
        first.organisation_id,
        access=access,
        delivery_stager=lambda _session, _dispatch: None,
        registry=AUTOMATION_REGISTRY,
    )

    with pytest.raises(AutomationServiceError, match="idempotency key") as error:
        await service.enqueue(second)

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_enqueue_rejects_cross_tenant_and_unavailable_producer_events() -> None:
    """Verify the contract that enqueue rejects cross tenant and unavailable producer events."""
    event = envelope()
    service = AutomationEventDispatchService(
        object(),  # type: ignore[arg-type]
        uuid4(),
        access=FakeAccess(),
        delivery_stager=lambda _session, _dispatch: None,
        registry=AUTOMATION_REGISTRY,
    )
    with pytest.raises(AutomationServiceError, match="organisation"):
        await service.enqueue(event)

    unavailable = event.model_copy(
        update={
            "event_key": "conversation.message.received",
            "source": event.source.model_copy(
                update={"domain": "conversation", "operation": "signal"}
            ),
            "subject": event.subject.model_copy(update={"type": "message"}),
            "record": {},
        }
    )
    unavailable_service = AutomationEventDispatchService(
        object(),  # type: ignore[arg-type]
        event.organisation_id,
        access=FakeAccess(),
        delivery_stager=lambda _session, _dispatch: None,
        registry=AUTOMATION_REGISTRY,
    )
    with pytest.raises(AutomationServiceError, match="producer is unavailable"):
        await unavailable_service.enqueue(unavailable)
