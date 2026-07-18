from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync.calendar import CalendarAttendee, CalendarEvent
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.email_models import Email, EmailDirection
from src.service.crm.discovery.contact_communication_hydration import (
    ContactCommunicationHydrationContext,
    ContactCommunicationHydrationService,
    SkipReason,
    build_contact_communication_hydration_plan,
)

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
INTEGRATION_ID = UUID("22222222-2222-2222-2222-222222222222")


def _context() -> ContactCommunicationHydrationContext:
    return ContactCommunicationHydrationContext(
        internal_emails={"seller@ourco.com", "calendar@ourco.com"},
        internal_domains={"ourco.com"},
    )


def _email(**kwargs) -> Email:
    return Email(
        organisation_id=ORG_ID,
        integration_id=INTEGRATION_ID,
        folder_id=uuid4(),
        email_id=kwargs.get("email_id", uuid4()),
        external_message_id=kwargs.get("external_message_id", "message-1"),
        from_email=kwargs.get("from_email", "buyer@example.com"),
        from_name=kwargs.get("from_name", "Buyer Person"),
        to_emails=kwargs.get("to_emails", ["seller@ourco.com"]),
        cc_emails=kwargs.get("cc_emails", []),
        bcc_emails=kwargs.get("bcc_emails", []),
        subject=kwargs.get("subject", "Hello"),
        received_at=kwargs.get("received_at", datetime(2026, 7, 2, 9, tzinfo=UTC)),
        direction=EmailDirection.INBOUND,
    )


def _event(**kwargs) -> CalendarEvent:
    event = CalendarEvent(
        organisation_id=ORG_ID,
        event_id=kwargs.get("event_id", uuid4()),
        external_id=kwargs.get("external_id", "event-1"),
        title=kwargs.get("title", "Review"),
        start_at=kwargs.get("start_at", datetime(2026, 7, 2, 11, tzinfo=UTC)),
        end_at=kwargs.get("end_at", datetime(2026, 7, 2, 12, tzinfo=UTC)),
        organiser_email=kwargs.get("organiser_email", "seller@ourco.com"),
        organiser_name=kwargs.get("organiser_name", "Seller"),
        attendee_count=2,
        status=kwargs.get("status", "confirmed"),
        is_all_day=False,
    )
    event.attendees = kwargs.get(
        "attendees",
        [
            CalendarAttendee(
                event_id=event.event_id,
                email="seller@ourco.com",
                display_name="Seller",
                response_status="accepted",
                is_organiser=True,
            ),
            CalendarAttendee(
                event_id=event.event_id,
                email="buyer@example.com",
                display_name="Buyer Person",
                response_status="accepted",
            ),
        ],
    )
    return event


def test_plan_deduplicates_people_across_email_and_calendar() -> None:
    email_id = uuid4()
    event_id = uuid4()

    plan = build_contact_communication_hydration_plan(
        emails=[_email(email_id=email_id, cc_emails=["buyer@example.com"])],
        events=[_event(event_id=event_id)],
        context=_context(),
    )

    assert [candidate.email for candidate in plan.candidates] == ["buyer@example.com"]
    assert plan.candidates[0].name == "Buyer Person"


def test_plan_skips_internal_role_and_resource_participants() -> None:
    event = _event(
        attendees=[
            CalendarAttendee(
                event_id=uuid4(),
                email="room@example.com",
                display_name="Board Room",
                response_status="accepted",
                data_payload={"resource": True},
            )
        ]
    )
    plan = build_contact_communication_hydration_plan(
        emails=[
            _email(
                from_email="seller@ourco.com",
                from_name="Seller",
                to_emails=[
                    "noreply@example.com",
                    "buyer@ourco.com",
                    "sales@example.com",
                ],
                cc_emails=["champion@example.com"],
            )
        ],
        events=[event],
        context=_context(),
    )

    assert [candidate.email for candidate in plan.candidates] == [
        "champion@example.com",
        "sales@example.com",
    ]
    assert "noreply@example.com" in plan.activity_identities
    assert plan.skip_counters[SkipReason.ROLE_MAILBOX.value] == 1
    assert plan.skip_counters[SkipReason.INTERNAL_DOMAIN.value] == 1
    assert plan.skip_counters[SkipReason.RESOURCE_ATTENDEE.value] == 1


def test_plan_does_not_turn_large_calendar_events_into_bulk_contact_imports() -> None:
    event = _event(
        attendees=[
            CalendarAttendee(
                event_id=uuid4(),
                email=f"person-{index}@example.com",
                response_status="accepted",
            )
            for index in range(26)
        ]
    )

    plan = build_contact_communication_hydration_plan(
        emails=[],
        events=[event],
        context=_context(),
    )

    assert plan.candidates == []
    assert plan.activity_identities == {f"person-{index}@example.com" for index in range(26)}
    assert plan.skip_counters[SkipReason.TOO_MANY_ATTENDEES.value] == 1


def test_plan_does_not_invent_contact_names_from_email_addresses() -> None:
    # Provider-local-part heuristics can silently persist a false human name.
    plan = build_contact_communication_hydration_plan(
        emails=[_email(from_email="first.last@example.com", from_name=None)],
        events=[],
        context=_context(),
    )

    assert plan.candidates[0].name is None


@pytest.mark.asyncio
async def test_context_uses_user_and_integration_addresses_as_exact_internal_identities() -> None:
    # A connected customer-domain mailbox must not hide every customer at that domain.
    users = MagicMock()
    users.scalars.return_value.all.return_value = ["employee@customer.com"]
    organisation = MagicMock()
    organisation.scalar_one_or_none.return_value = ["ourco.com"]
    integrations = MagicMock()
    integrations.scalars.return_value.all.return_value = ["owner@partner.com"]
    org_session = MagicMock()
    org_session.execute = AsyncMock(return_value=integrations)
    master_session = MagicMock()
    master_session.execute = AsyncMock(side_effect=[users, organisation])
    service = ContactCommunicationHydrationService(org_session, master_session, ORG_ID)

    context = await service._load_context()

    assert context.internal_emails == {
        "employee@customer.com",
        "owner@partner.com",
    }
    assert context.internal_domains == {"ourco.com"}


@pytest.mark.asyncio
async def test_changed_source_links_suppressed_identity_when_contact_already_exists() -> None:
    # Creation suppression must not also suppress history for a deliberately created contact.
    email = _email(
        from_email="seller@ourco.com",
        from_name="Seller",
        to_emails=["noreply@example.com"],
    )
    contact = CRMContact(
        organisation_id=ORG_ID,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="noreply@example.com",
        is_deleted=False,
    )
    service = ContactCommunicationHydrationService(MagicMock(), MagicMock(), ORG_ID)
    service._load_changed_emails = AsyncMock(return_value=[email])  # type: ignore[method-assign]
    service._load_changed_events = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service._load_context = AsyncMock(return_value=_context())  # type: ignore[method-assign]
    service._project = AsyncMock(return_value=1)  # type: ignore[method-assign]
    writer = MagicMock()
    writer.find_or_create_contacts_by_email_batch = AsyncMock(return_value={})
    writer.find_contacts_by_email_batch = AsyncMock(return_value={"noreply@example.com": contact})

    with patch(
        "src.service.crm.discovery.contact_communication_hydration.ContactWriteService",
        return_value=writer,
    ):
        result = await service.hydrate_changed_sources(changed_email_ids=[email.email_id])

    assert result.contacts_created == 0
    assert result.contacts_reused == 1
    assert service._project.await_args.kwargs["identity_contacts"] == {  # type: ignore[attr-defined]
        "noreply@example.com": contact
    }


@pytest.mark.asyncio
async def test_missing_explicit_contacts_do_not_fall_through_to_source_hydration() -> None:
    service = ContactCommunicationHydrationService(MagicMock(), MagicMock(), ORG_ID)
    service._load_contacts = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service._load_changed_emails = AsyncMock()  # type: ignore[method-assign]
    service._load_changed_events = AsyncMock()  # type: ignore[method-assign]

    result = await service.backfill_contacts([uuid4()])

    assert result.contacts_reused == 0
    service._load_changed_emails.assert_not_awaited()
    service._load_changed_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_contact_backfill_email_query_has_constant_jsonb_predicates() -> None:
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    service = ContactCommunicationHydrationService(session, MagicMock(), ORG_ID)

    await service._load_emails_for_identities(
        {f"person-{index}@example.com" for index in range(100)}
    )

    statement = session.execute.await_args.args[0]
    assert str(statement).count(" ?| ") == 3
