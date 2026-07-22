"""Contracts for transaction-local automation event enqueueing."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import AutomationEventDispatch
from src.service.automation.errors import AutomationServiceError
from src.service.automation.events.service import AutomationEventDispatchService


class FakeAccess:
    def __init__(self, *, inserted: bool = True) -> None:
        self.inserted = inserted
        self.rows: dict[tuple[str, str, str], AutomationEventDispatch] = {}

    async def insert_if_absent(self, dispatch: AutomationEventDispatch) -> bool:
        key = (dispatch.source_domain, dispatch.source_event_id, dispatch.event_type)
        if self.inserted:
            self.rows[key] = dispatch
        return self.inserted

    async def get_by_source(
        self, *, source_domain: str, source_event_id: str, event_type: str
    ) -> AutomationEventDispatch | None:
        return self.rows.get((source_domain, source_event_id, event_type))


@pytest.mark.asyncio
async def test_enqueue_returns_one_dispatch_for_supported_event() -> None:
    """A supported semantic occurrence gets one durable activation identity."""

    organisation_id = uuid4()
    access = FakeAccess()
    staged: list[AutomationEventDispatch] = []
    service = AutomationEventDispatchService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        delivery_stager=lambda _session, dispatch: staged.append(dispatch),
    )
    source_event_id = str(uuid4())

    dispatch = await service.enqueue(
        source_domain="crm",
        source_event_id=source_event_id,
        event_type="crm.contact.created",
    )

    assert dispatch.organisation_id == organisation_id
    assert dispatch.source_domain == "crm"
    assert dispatch.source_event_id == source_event_id
    assert dispatch.event_type == "crm.contact.created"
    assert staged == [dispatch]


@pytest.mark.asyncio
async def test_enqueue_returns_existing_dispatch_on_replay() -> None:
    """A mutation retry repairs publication without duplicating activation state."""

    organisation_id = uuid4()
    source_event_id = str(uuid4())
    existing = AutomationEventDispatch(
        organisation_id=organisation_id,
        source_domain="crm",
        source_event_id=source_event_id,
        event_type="crm.contact.created",
    )
    access = FakeAccess(inserted=False)
    access.rows[("crm", source_event_id, "crm.contact.created")] = existing
    staged: list[AutomationEventDispatch] = []
    service = AutomationEventDispatchService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        delivery_stager=lambda _session, dispatch: staged.append(dispatch),
    )

    replay = await service.enqueue(
        source_domain="crm",
        source_event_id=source_event_id,
        event_type="crm.contact.created",
    )

    assert replay.dispatch_id == existing.dispatch_id
    assert staged == [existing]


@pytest.mark.asyncio
async def test_enqueue_rejects_unsupported_event_contract() -> None:
    """Internal producers cannot manufacture event types absent from the catalog contract."""

    service = AutomationEventDispatchService(
        object(),  # type: ignore[arg-type]
        uuid4(),
        access=FakeAccess(),
        delivery_stager=lambda _session, _dispatch: None,
    )

    with pytest.raises(AutomationServiceError, match="Unsupported automation event"):
        await service.enqueue(
            source_domain="gmail",
            source_event_id="message-1",
            event_type="gmail.message.received",
        )


def test_dispatch_identity_accepts_non_uuid_provider_ids() -> None:
    """Future Gmail events retain their provider ID without UUID coercion."""

    row = AutomationEventDispatch(
        organisation_id=uuid4(),
        source_domain="gmail",
        source_event_id="18f-provider-message-id",
        event_type="gmail.message.received",
    )

    assert isinstance(row.dispatch_id, UUID)
    assert row.source_event_id == "18f-provider-message-id"
