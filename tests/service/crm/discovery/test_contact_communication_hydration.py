"""Regression tests for relationship-based contact creation and activity hydration."""

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
        thread_id=kwargs.get("thread_id"),
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
    """One identity appearing in email and calendar must produce one candidate."""
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
    """Internal, functional, and resource identities must not become contacts."""
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

    assert [candidate.email for candidate in plan.candidates] == ["champion@example.com"]
    assert "noreply@example.com" in plan.activity_identities
    assert plan.skip_counters[SkipReason.ROLE_MAILBOX.value] == 2
    assert plan.skip_counters[SkipReason.INTERNAL_DOMAIN.value] == 1
    assert plan.skip_counters[SkipReason.RESOURCE_ATTENDEE.value] == 1


def test_plan_does_not_turn_large_calendar_events_into_bulk_contact_imports() -> None:
    """Large meetings must remain activity-only instead of acting as contact imports."""
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
    """Missing provider names must remain absent instead of being guessed from email."""
    plan = build_contact_communication_hydration_plan(
        emails=[
            _email(
                from_email="seller@ourco.com",
                from_name="Seller",
                to_emails=["first.last@example.com"],
            )
        ],
        events=[],
        context=_context(),
    )

    assert plan.candidates[0].name is None


def test_plan_keeps_first_and_repeated_inbound_outreach_unengaged() -> None:
    """Repeated inbound outreach must remain latent until a coworker responds."""
    plan = build_contact_communication_hydration_plan(
        emails=[
            _email(
                external_message_id=f"message-{index}",
                thread_id="cold-thread",
            )
            for index in range(3)
        ],
        events=[],
        context=_context(),
    )

    assert plan.candidates == []
    assert plan.activity_identities == {"buyer@example.com"}
    assert plan.skip_counters[SkipReason.INBOUND_UNENGAGED.value] == 1


def test_plan_creates_every_external_recipient_from_internal_outbound() -> None:
    """Intentional outbound mail must create external To, CC, and BCC recipients."""
    plan = build_contact_communication_hydration_plan(
        emails=[
            _email(
                from_email="seller@ourco.com",
                from_name="Seller",
                to_emails=["buyer@example.com"],
                cc_emails=["champion@example.com"],
                bcc_emails=["advisor@example.com"],
            )
        ],
        events=[],
        context=_context(),
    )

    assert [candidate.email for candidate in plan.candidates] == [
        "advisor@example.com",
        "buyer@example.com",
        "champion@example.com",
    ]


def test_plan_promotes_all_thread_participants_after_internal_reply() -> None:
    """One internal response must promote every eligible participant in the thread."""
    plan = build_contact_communication_hydration_plan(
        emails=[
            _email(
                external_message_id="inbound",
                thread_id="engaged-thread",
                cc_emails=["champion@example.com"],
            ),
            _email(
                external_message_id="reply",
                thread_id="engaged-thread",
                from_email="seller@ourco.com",
                from_name="Seller",
                to_emails=["buyer@example.com"],
            ),
        ],
        events=[],
        context=_context(),
    )

    assert [candidate.email for candidate in plan.candidates] == [
        "buyer@example.com",
        "champion@example.com",
    ]
    assert SkipReason.INBOUND_UNENGAGED.value not in plan.skip_counters


def test_plan_does_not_promote_an_unrelated_inbound_thread() -> None:
    """A sent message in another provider thread must not promote cold outreach."""
    plan = build_contact_communication_hydration_plan(
        emails=[
            _email(
                external_message_id="cold",
                thread_id="cold-thread",
                from_email="cold@example.com",
            ),
            _email(
                external_message_id="outbound",
                thread_id="outbound-thread",
                from_email="seller@ourco.com",
                from_name="Seller",
                to_emails=["prospect@example.com"],
            ),
        ],
        events=[],
        context=_context(),
    )

    assert [candidate.email for candidate in plan.candidates] == ["prospect@example.com"]
    assert plan.activity_identities == {"cold@example.com", "prospect@example.com"}
    assert plan.skip_counters[SkipReason.INBOUND_UNENGAGED.value] == 1


@pytest.mark.parametrize(
    "address",
    [
        "marketing+india@example.com",
        "support-emea@example.com",
        "sales.notifications@example.com",
        "apply@example.com",
        "insider@example.com",
    ],
)
def test_plan_suppresses_normalized_functional_mailboxes(address: str) -> None:
    """Alias suffixes and regional variants must not bypass the role-mailbox guard."""
    plan = build_contact_communication_hydration_plan(
        emails=[
            _email(
                from_email="seller@ourco.com",
                to_emails=[address],
            )
        ],
        events=[],
        context=_context(),
    )

    assert plan.candidates == []
    assert plan.skip_counters[SkipReason.ROLE_MAILBOX.value] == 1


def test_plan_does_not_substring_match_personal_mailboxes() -> None:
    """Role words embedded inside a personal local-part token must remain eligible."""
    plan = build_contact_communication_hydration_plan(
        emails=[
            _email(
                from_email="seller@ourco.com",
                to_emails=["salesforce@example.com", "supporter@example.com"],
            )
        ],
        events=[],
        context=_context(),
    )

    assert [candidate.email for candidate in plan.candidates] == [
        "salesforce@example.com",
        "supporter@example.com",
    ]


@pytest.mark.asyncio
async def test_context_uses_user_and_integration_addresses_as_exact_internal_identities() -> None:
    """Connected addresses must not make their entire external domains internal."""
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
    """Creation suppression must not hide history from a deliberate existing contact."""
    email = _email()
    contact = CRMContact(
        organisation_id=ORG_ID,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@example.com",
        is_deleted=False,
    )
    service = ContactCommunicationHydrationService(MagicMock(), MagicMock(), ORG_ID)
    service._load_changed_emails = AsyncMock(return_value=[email])  # type: ignore[method-assign]
    service._load_email_thread_context = AsyncMock(  # type: ignore[method-assign]
        return_value=[email]
    )
    service._load_changed_events = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service._load_context = AsyncMock(return_value=_context())  # type: ignore[method-assign]
    service._project = AsyncMock(return_value=1)  # type: ignore[method-assign]
    writer = MagicMock()
    writer.find_or_create_contacts_by_email_batch = AsyncMock(return_value={})
    writer.find_contacts_by_email_batch = AsyncMock(return_value={"buyer@example.com": contact})

    with patch(
        "src.service.crm.discovery.contact_communication_hydration.ContactWriteService",
        return_value=writer,
    ):
        result = await service.hydrate_changed_sources(changed_email_ids=[email.email_id])

    assert result.contacts_created == 0
    assert result.contacts_reused == 1
    assert service._project.await_args.kwargs["identity_contacts"] == {  # type: ignore[attr-defined]
        "buyer@example.com": contact
    }


@pytest.mark.asyncio
async def test_thread_context_loads_provider_thread_and_deduplicates_changed_row() -> None:
    """A changed row must expand to its retained provider thread without duplication."""
    changed = _email(
        external_message_id="changed",
        thread_id="provider-thread",
    )
    earlier = _email(
        external_message_id="parent",
        thread_id="provider-thread",
        from_email="seller@ourco.com",
        to_emails=["buyer@example.com"],
    )
    service = ContactCommunicationHydrationService(MagicMock(), MagicMock(), ORG_ID)
    service._load_email_context_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=[earlier, changed]
    )

    result = await service._load_email_thread_context([changed])

    assert {email.email_id for email in result} == {
        changed.email_id,
        earlier.email_id,
    }
    service._load_email_context_rows.assert_awaited_once_with(  # type: ignore[attr-defined]
        {(INTEGRATION_ID, "provider-thread")}
    )


@pytest.mark.asyncio
async def test_thread_context_query_pairs_provider_thread_with_integration() -> None:
    """Provider thread IDs must remain integration-scoped inside the tenant boundary."""
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(return_value=rows)
    service = ContactCommunicationHydrationService(session, MagicMock(), ORG_ID)

    await service._load_email_context_rows({(INTEGRATION_ID, "provider-thread")})

    statement = str(session.execute.await_args.args[0])
    assert "emails.organisation_id" in statement
    assert "(emails.integration_id, emails.thread_id) IN" in statement


@pytest.mark.asyncio
async def test_missing_explicit_contacts_do_not_fall_through_to_source_hydration() -> None:
    """Missing explicit contacts must not accidentally invoke changed-source mode."""
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
    """Identity backfill must use constant JSONB predicates as the contact batch grows."""
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
