from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.service.crm.discovery.calendar_contact_account_discovery import (
    CalendarContactAccountDiscoveryService,
    CalendarDiscoveryAttendee,
    CalendarDiscoveryContext,
    CalendarDiscoveryEvent,
    CalendarDiscoveryResult,
    SkipReason,
    build_discovery_plan,
)

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
INTEGRATION_ID = UUID("22222222-2222-2222-2222-222222222222")


class _FakeResult:
    def __init__(self, values: list[object] | object | None) -> None:
        self._values = values

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[object]:
        if isinstance(self._values, list):
            return self._values
        if self._values is None:
            return []
        return [self._values]

    def scalar_one_or_none(self) -> object | None:
        return self._values


class _FakeSession:
    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = results

    async def execute(self, _stmt: object) -> _FakeResult:
        return self._results.pop(0)


def _event(*, attendees: list[CalendarDiscoveryAttendee], **kwargs) -> CalendarDiscoveryEvent:
    return CalendarDiscoveryEvent(
        event_id=kwargs.get("event_id", uuid4()),
        start_at=kwargs.get("start_at", datetime(2026, 5, 30, 15, tzinfo=UTC)),
        user_state_updated_at=kwargs.get(
            "user_state_updated_at", datetime(2026, 5, 30, 14, tzinfo=UTC)
        ),
        status=kwargs.get("status", "confirmed"),
        is_all_day=kwargs.get("is_all_day", False),
        attendees=attendees,
    )


def _attendee(
    email: str,
    *,
    name: str | None = "Ada Lovelace",
    response_status: str = "accepted",
    is_organiser: bool = False,
) -> CalendarDiscoveryAttendee:
    return CalendarDiscoveryAttendee(
        email=email,
        display_name=name,
        response_status=response_status,
        is_organiser=is_organiser,
    )


def _context() -> CalendarDiscoveryContext:
    return CalendarDiscoveryContext(
        organisation_id=ORG_ID,
        source_integration_id=INTEGRATION_ID,
        internal_emails={"seller@acme.com", "calendar@acme.com"},
        internal_domains={"acme.com"},
    )


def test_build_discovery_plan_creates_account_before_contact_from_calendar_attendees() -> None:
    event_id = uuid4()
    plan = build_discovery_plan(
        events=[
            _event(
                event_id=event_id,
                attendees=[
                    _attendee("seller@acme.com", name="Internal Seller"),
                    _attendee("ada@customer.io", name="Ada Lovelace"),
                ],
            )
        ],
        context=_context(),
        existing_contacts={},
        existing_company_domains=set(),
    )

    assert [candidate.domain for candidate in plan.accounts] == ["customer.io"]
    assert plan.accounts[0].properties["created_via"] == "calendar_initial_discovery"
    assert plan.accounts[0].properties["source_integration_id"] == str(INTEGRATION_ID)
    assert plan.accounts[0].properties["evidence_event_ids"] == [str(event_id)]

    assert [candidate.email for candidate in plan.contacts] == ["ada@customer.io"]
    assert plan.contacts[0].full_name == "Ada Lovelace"
    assert plan.contacts[0].company_domain == "customer.io"


@pytest.mark.parametrize(
    ("attendee", "reason"),
    [
        (_attendee("noreply@customer.io"), SkipReason.ROLE_MAILBOX),
        (_attendee("support@customer.io"), SkipReason.ROLE_MAILBOX),
        (_attendee("ada@gmail.com"), SkipReason.FREE_EMAIL_DOMAIN),
        (_attendee("ada@itbh.ac.in"), SkipReason.ACADEMIC_DOMAIN),
        (_attendee("ada@cs.usc.edu"), SkipReason.ACADEMIC_DOMAIN),
        (_attendee("ada@gov.in"), SkipReason.GOVERNMENT_DOMAIN),
        (_attendee("ada@mailinator.com"), SkipReason.DISPOSABLE_EMAIL_DOMAIN),
        (_attendee("seller@acme.com"), SkipReason.INTERNAL_EMAIL),
        (_attendee("buyer@acme.com"), SkipReason.INTERNAL_DOMAIN),
        (_attendee("ada@customer.io", response_status="Declined"), SkipReason.DECLINED),
        (_attendee("ada@customer.io", is_organiser=True), SkipReason.ORGANIZER),
        (
            CalendarDiscoveryAttendee(
                email="room-101@customer.io",
                display_name="Room 101",
                response_status="accepted",
                is_organiser=False,
                is_resource=True,
            ),
            SkipReason.RESOURCE_ATTENDEE,
        ),
        (_attendee("not-an-email"), SkipReason.MALFORMED_EMAIL),
        (_attendee("ada@customer..io"), SkipReason.MALFORMED_EMAIL),
        (_attendee("ada@customer.io/path"), SkipReason.MALFORMED_EMAIL),
        (_attendee("ada@localhost"), SkipReason.MALFORMED_EMAIL),
        (_attendee("ada@bad-.customer.io"), SkipReason.MALFORMED_EMAIL),
        (_attendee("ada@-bad.customer.io"), SkipReason.MALFORMED_EMAIL),
        (_attendee("person@customer.io", name="Unknown"), SkipReason.MISSING_NAME),
    ],
)
def test_build_discovery_plan_skips_risky_attendees(
    attendee: CalendarDiscoveryAttendee,
    reason: SkipReason,
) -> None:
    plan = build_discovery_plan(
        events=[
            _event(
                attendees=[
                    _attendee("seller@acme.com", name="Internal Seller"),
                    attendee,
                ],
            )
        ],
        context=_context(),
        existing_contacts={},
        existing_company_domains=set(),
    )

    assert plan.accounts == []
    assert plan.contacts == []
    assert plan.skip_counters[reason.value] >= 1


def test_build_discovery_plan_skips_attendees_from_triggering_integration_domain() -> None:
    context = CalendarDiscoveryContext(
        organisation_id=ORG_ID,
        source_integration_id=INTEGRATION_ID,
        internal_emails={"seller@acme.com", "calendar@calendar-acme.com"},
        internal_domains={"acme.com", "calendar-acme.com"},
    )

    plan = build_discovery_plan(
        events=[
            _event(
                attendees=[
                    _attendee("seller@acme.com", name="Internal Seller"),
                    _attendee("buyer@calendar-acme.com", name="Internal Buyer"),
                ],
            )
        ],
        context=context,
        existing_contacts={},
        existing_company_domains=set(),
    )

    assert plan.accounts == []
    assert plan.contacts == []
    assert plan.skip_counters[SkipReason.INTERNAL_DOMAIN.value] == 1


@pytest.mark.asyncio
async def test_load_context_marks_org_user_and_calendar_integration_domains_internal() -> None:
    service = CalendarContactAccountDiscoveryService(
        org_session=_FakeSession(
            [_FakeResult(["calendar@calendar-acme.com", "personal@gmail.com"])]
        ),
        master_session=_FakeSession(
            [
                _FakeResult(["seller@acme.com", "teammate@internal.acme.com"]),
                _FakeResult(["https://www.Acme.com"]),
            ]
        ),
        organisation_id=ORG_ID,
        source_integration_id=INTEGRATION_ID,
    )

    context = await service._load_context()

    assert "acme.com" in context.internal_domains
    assert "internal.acme.com" in context.internal_domains
    assert "calendar-acme.com" in context.internal_domains
    assert "gmail.com" not in context.internal_domains


@pytest.mark.parametrize(
    ("event_kwargs", "reason"),
    [
        ({"status": "cancelled"}, SkipReason.CANCELLED_EVENT),
        ({"is_all_day": True}, SkipReason.ALL_DAY_EVENT),
    ],
)
def test_build_discovery_plan_skips_non_meeting_events(
    event_kwargs: dict, reason: SkipReason
) -> None:
    plan = build_discovery_plan(
        events=[
            _event(
                attendees=[
                    _attendee("seller@acme.com", name="Internal Seller"),
                    _attendee("ada@customer.io", name="Ada Lovelace"),
                ],
                **event_kwargs,
            )
        ],
        context=_context(),
        existing_contacts={},
        existing_company_domains=set(),
    )

    assert plan.accounts == []
    assert plan.contacts == []
    assert plan.skip_counters[reason.value] == 1


def test_build_discovery_plan_requires_internal_participant_evidence() -> None:
    plan = build_discovery_plan(
        events=[_event(attendees=[_attendee("ada@customer.io", name="Ada Lovelace")])],
        context=_context(),
        existing_contacts={},
        existing_company_domains=set(),
    )

    assert plan.accounts == []
    assert plan.contacts == []
    assert plan.skip_counters[SkipReason.NO_INTERNAL_PARTICIPANT.value] == 1


def test_build_discovery_plan_skips_large_events_as_webinar_like() -> None:
    attendees = [_attendee("seller@acme.com", name="Internal Seller")]
    attendees.extend(
        _attendee(f"person{index}@customer{index}.io", name=f"Person {index}")
        for index in range(1, 31)
    )

    plan = build_discovery_plan(
        events=[_event(attendees=attendees)],
        context=_context(),
        existing_contacts={},
        existing_company_domains=set(),
    )

    assert plan.accounts == []
    assert plan.contacts == []
    assert plan.skip_counters[SkipReason.TOO_MANY_ATTENDEES.value] == 1


def test_build_discovery_plan_is_idempotent_across_existing_crm_rows() -> None:
    plan = build_discovery_plan(
        events=[
            _event(
                attendees=[
                    _attendee("seller@acme.com", name="Internal Seller"),
                    _attendee("ada@customer.io", name="Ada Lovelace"),
                    _attendee("grace@existing.io", name="Grace Hopper"),
                ],
            )
        ],
        context=_context(),
        existing_contacts={"ada@customer.io": None},
        existing_company_domains={"existing.io"},
    )

    assert plan.accounts == []
    assert plan.contacts == []
    assert plan.skip_counters[SkipReason.EXISTING_CONTACT.value] == 1
    assert plan.skip_counters[SkipReason.EXISTING_ACCOUNT.value] == 1


def test_build_discovery_plan_skips_existing_contact_newer_than_event() -> None:
    event_start = datetime(2026, 5, 30, 15, tzinfo=UTC)
    plan = build_discovery_plan(
        events=[
            _event(
                start_at=event_start,
                attendees=[
                    _attendee("seller@acme.com", name="Internal Seller"),
                    _attendee("ada@customer.io", name="Ada Lovelace"),
                ],
            )
        ],
        context=_context(),
        existing_contacts={"ada@customer.io": datetime(2026, 5, 31, 10, tzinfo=UTC)},
        existing_company_domains=set(),
    )

    assert plan.accounts == []
    assert plan.contacts == []
    assert plan.skip_counters[SkipReason.EXISTING_CONTACT_NEWER_THAN_EVENT.value] == 1


def test_calendar_discovery_result_carries_processed_event_watermark() -> None:
    min_updated_at = datetime(2026, 5, 30, 14, tzinfo=UTC)
    max_updated_at = datetime(2026, 5, 31, 10, tzinfo=UTC)

    result = CalendarDiscoveryResult(
        events_considered=2,
        min_event_user_state_updated_at=min_updated_at,
        max_event_user_state_updated_at=max_updated_at,
        accounts_created=1,
        contacts_created=1,
        accounts_skipped=0,
        contacts_skipped=0,
        skip_counters={},
    )

    assert result.events_considered == 2
    assert result.min_event_user_state_updated_at == min_updated_at
    assert result.max_event_user_state_updated_at == max_updated_at


def test_build_discovery_plan_derives_human_name_from_email_when_display_name_missing() -> None:
    plan = build_discovery_plan(
        events=[
            _event(
                attendees=[
                    _attendee("seller@acme.com", name="Internal Seller"),
                    _attendee("grace.hopper@customer.io", name=None),
                ],
            )
        ],
        context=_context(),
        existing_contacts={},
        existing_company_domains=set(),
    )

    assert [candidate.full_name for candidate in plan.contacts] == ["Grace Hopper"]
    assert plan.contacts[0].first_name == "Grace"
    assert plan.contacts[0].last_name == "Hopper"
