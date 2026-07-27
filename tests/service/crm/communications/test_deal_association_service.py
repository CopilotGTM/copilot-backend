"""Business-rule tests for domain-owned communication-to-deal association."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import src.service.crm.communications.deal_association_service as association_module
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from src.service.crm.communications.deal_association_service import (
    CommunicationCandidate,
    CommunicationDealAssociationService,
    DealCandidate,
    communication_matches_deal,
    resolve_unique_deal_id,
)
from src.service.crm.communications.participant_resolution_service import (
    ContactCommunicationHydrationContext,
)


def test_resolves_only_deal_with_contact_and_internal_participant() -> None:
    """A contact match is insufficient unless the communication also includes the deal team."""
    contact_id = uuid4()
    expected_deal_id = uuid4()
    communication = CommunicationCandidate(
        contact_ids=frozenset({contact_id}),
        participant_emails=frozenset({"owner@acme.test", "buyer@customer.test"}),
    )

    result = resolve_unique_deal_id(
        communication,
        [
            DealCandidate(
                deal_id=expected_deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            ),
            DealCandidate(
                deal_id=uuid4(),
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"other@acme.test"}),
            ),
        ],
    )

    assert result == expected_deal_id


def test_rejects_communication_matching_multiple_deals() -> None:
    """Ambiguous relationship activity must stay on contact/account feeds."""
    contact_id = uuid4()
    communication = CommunicationCandidate(
        contact_ids=frozenset({contact_id}),
        participant_emails=frozenset({"owner@acme.test", "buyer@customer.test"}),
    )

    result = resolve_unique_deal_id(
        communication,
        [
            DealCandidate(
                deal_id=uuid4(),
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            ),
            DealCandidate(
                deal_id=uuid4(),
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            ),
        ],
    )

    assert result is None


def test_rejects_contact_match_without_deal_team_participant() -> None:
    """An unrelated employee's conversation cannot become deal activity."""
    contact_id = uuid4()

    result = resolve_unique_deal_id(
        CommunicationCandidate(
            contact_ids=frozenset({contact_id}),
            participant_emails=frozenset({"other@acme.test", "buyer@customer.test"}),
        ),
        [
            DealCandidate(
                deal_id=uuid4(),
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
        ],
    )

    assert result is None


def test_existing_association_is_not_a_current_match_by_itself() -> None:
    """Historical state must not bypass the current contact and team rule."""
    result = resolve_unique_deal_id(
        CommunicationCandidate(
            contact_ids=frozenset(),
            participant_emails=frozenset(),
            existing_deal_ids=frozenset({uuid4()}),
        ),
        [],
    )

    assert result is None


def test_existing_association_must_still_satisfy_current_participant_rule() -> None:
    """Removing the deal contact must invalidate an old automatic association."""
    stale_deal_id = uuid4()
    communication = CommunicationCandidate(
        contact_ids=frozenset({uuid4()}),
        participant_emails=frozenset({"buyer@customer.test", "seller@acme.test"}),
        existing_deal_ids=frozenset({stale_deal_id}),
    )

    assert (
        communication_matches_deal(
            communication,
            DealCandidate(
                deal_id=stale_deal_id,
                contact_ids=frozenset(),
                internal_emails=frozenset({"seller@acme.test"}),
            ),
        )
        is False
    )


def test_does_not_collapse_multiple_existing_associations() -> None:
    """Multiple explicit links remain explicit instead of selecting one arbitrarily."""
    result = resolve_unique_deal_id(
        CommunicationCandidate(
            contact_ids=frozenset(),
            participant_emails=frozenset(),
            existing_deal_ids=frozenset({uuid4(), uuid4()}),
        ),
        [],
    )

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_attachments", "expects_attachment_deal"),
    [
        (True, True),
        ("true", True),
        (False, False),
        ("false", False),
        (None, False),
        ("TRUE", False),
    ],
)
async def test_changed_email_tracks_only_canonical_attachment_flags(
    has_attachments,
    expects_attachment_deal: bool,
) -> None:
    """Only the provider-normalized attachment flag should enqueue deal asset work."""
    contact_id = uuid4()
    deal_id = uuid4()
    email = SimpleNamespace(
        email_id=uuid4(),
        internet_message_id="message-1",
        from_email="owner@acme.test",
        to_emails=["buyer@customer.test"],
        cc_emails=[],
        bcc_emails=[],
        has_attachments=has_attachments,
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
        ]
    )
    service._associate_email = AsyncMock(return_value=True)  # type: ignore[method-assign]
    service._associate_event = AsyncMock()  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        attachment_scope_email_ids={email.email_id},
    )

    assert result.associations_created == 1
    assert result.ambiguous == 0
    assert result.email_deal_ids == {deal_id}
    assert result.attachment_email_ids_by_deal == (
        {deal_id: {email.email_id}} if expects_attachment_deal else {}
    )
    service._associate_email.assert_awaited_once_with(  # type: ignore[attr-defined]
        email,
        deal_id=deal_id,
        contact_ids={contact_id},
        contact_emails={"buyer@customer.test"},
    )


@pytest.mark.asyncio
async def test_thread_context_attachment_outside_scope_does_not_emit_work() -> None:
    """Thread context may aid matching but must not widen provider attachment work."""
    contact_id = uuid4()
    deal_id = uuid4()
    changed_email = SimpleNamespace(
        email_id=uuid4(),
        internet_message_id="message-current",
        from_email="owner@acme.test",
        to_emails=["buyer@customer.test"],
        cc_emails=[],
        bcc_emails=[],
        has_attachments=False,
    )
    historical_email = SimpleNamespace(
        email_id=uuid4(),
        internet_message_id="message-history",
        from_email="owner@acme.test",
        to_emails=["buyer@customer.test"],
        cc_emails=[],
        bcc_emails=[],
        has_attachments=True,
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
        ]
    )
    service._associate_email = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[changed_email, historical_email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        attachment_scope_email_ids={changed_email.email_id},
    )

    assert result.email_deal_ids == {deal_id}
    assert result.attachment_email_ids_by_deal == {}


@pytest.mark.asyncio
async def test_existing_email_association_still_tracks_attachment_recovery() -> None:
    """Historical replay must recover assets even when the deal link already exists."""
    contact_id = uuid4()
    deal_id = uuid4()
    email = SimpleNamespace(
        email_id=uuid4(),
        internet_message_id="message-1",
        from_email="owner@acme.test",
        to_emails=["buyer@customer.test"],
        cc_emails=[],
        bcc_emails=[],
        has_attachments=True,
        received_at=None,
        sent_at=None,
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(  # type: ignore[method-assign]
        return_value=({"message-1": {deal_id}}, {})
    )
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
        ]
    )
    service._update_last_contact = AsyncMock()  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        discover_participants=False,
        attachment_scope_email_ids={email.email_id},
    )

    assert result.email_deal_ids == {deal_id}
    assert result.attachment_email_ids_by_deal == {deal_id: {email.email_id}}
    service._update_last_contact.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stale_email_association_does_not_trigger_deal_followups() -> None:
    """An association without a current eligible deal must not launch downstream work."""
    contact_id = uuid4()
    stale_deal_id = uuid4()
    email = SimpleNamespace(
        email_id=uuid4(),
        internet_message_id="message-1",
        from_email="owner@acme.test",
        to_emails=["buyer@customer.test"],
        cc_emails=[],
        bcc_emails=[],
        has_attachments=True,
        received_at=None,
        sent_at=None,
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(  # type: ignore[method-assign]
        return_value=({"message-1": {stale_deal_id}}, {})
    )
    service._load_deal_candidates = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service._update_last_contact = AsyncMock()  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        attachment_scope_email_ids={email.email_id},
    )

    assert result.unmatched == 1
    assert result.email_deal_ids == set()
    assert result.attachment_email_ids_by_deal == {}
    service._update_last_contact.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_changed_email_associates_every_matching_deal(monkeypatch) -> None:
    """New cards refresh each matching deal once after the whole batch."""
    contact_id = uuid4()
    deal_ids = {uuid4(), uuid4()}
    emails = [
        SimpleNamespace(
            email_id=uuid4(),
            internet_message_id=f"message-{index}",
            from_email="owner@acme.test",
            to_emails=["buyer@customer.test"],
            cc_emails=[],
            bcc_emails=[],
            has_attachments=True,
        )
        for index in range(2)
    ]
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
            for deal_id in deal_ids
        ]
    )
    service._associate_email = AsyncMock(return_value=True)  # type: ignore[method-assign]
    service._associate_event = AsyncMock()  # type: ignore[method-assign]
    refresher = MagicMock()
    refresher.refresh_deal = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "DealLastActivityService",
        lambda *_args: refresher,
        raising=False,
    )

    result = await service.associate_changed_communications(
        emails=emails,
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        attachment_scope_email_ids={email.email_id for email in emails},
    )

    assert result.associations_created == 4
    assert result.ambiguous == 0
    assert result.email_deal_ids == deal_ids
    assert result.attachment_email_ids_by_deal == {
        deal_id: {email.email_id for email in emails} for deal_id in deal_ids
    }
    assert {
        call.kwargs["deal_id"]
        for call in service._associate_email.await_args_list  # type: ignore[attr-defined]
    } == deal_ids
    assert refresher.refresh_deal.await_count == 2
    assert {call.args[0] for call in refresher.refresh_deal.await_args_list} == deal_ids


@pytest.mark.asyncio
async def test_changed_meeting_associates_every_matching_deal() -> None:
    """One meeting may be valid activity for multiple deals sharing its participants."""
    contact_id = uuid4()
    deal_ids = {uuid4(), uuid4()}
    event = SimpleNamespace(
        event_id=uuid4(),
        status="confirmed",
        organiser_email="owner@acme.test",
        attendees=[SimpleNamespace(email="buyer@customer.test")],
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._cancel_event_associations = AsyncMock(return_value=0)  # type: ignore[method-assign]
    service._load_existing_deal_ids = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
            for deal_id in deal_ids
        ]
    )
    service._associate_event = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[],
        events=[event],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        attachment_scope_email_ids=set(),
    )

    assert result.associations_created == 2
    assert result.ambiguous == 0
    assert {
        call.kwargs["deal_id"]
        for call in service._associate_event.await_args_list  # type: ignore[attr-defined]
    } == deal_ids


@pytest.mark.asyncio
async def test_association_returns_stakeholder_candidates_without_writing_participants() -> None:
    """Communication policy should describe participant writes, not perform them."""
    contact_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    email = SimpleNamespace(
        email_id=uuid4(),
        internet_message_id="message-1",
        from_email="owner@acme.test",
        to_emails=["buyer@customer.test"],
        cc_emails=[],
        bcc_emails=[],
        has_attachments=False,
    )
    session = MagicMock()
    service = CommunicationDealAssociationService(session, uuid4())
    service._load_existing_deal_ids = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=deal_id,
                integration_id=integration_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
                customer_domains=frozenset({"customer.test"}),
            )
        ]
    )
    service._associate_email = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={"buyer@customer.test": SimpleNamespace(contact_id=contact_id)},
        integration_id=None,
        attachment_scope_email_ids={email.email_id},
    )

    assert len(result.stakeholder_candidates) == 1
    candidates = result.stakeholder_candidates[0]
    assert candidates.deal_id == deal_id
    assert candidates.integration_id == integration_id
    assert candidates.contact_emails_by_id == {contact_id: "buyer@customer.test"}
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_stakeholder_candidate_uses_customer_identity_from_matched_communication() -> None:
    """Discovery should keep the matched customer alias instead of another known address."""
    contact_id = uuid4()
    deal_id = uuid4()
    contact = SimpleNamespace(contact_id=contact_id)
    email = SimpleNamespace(
        email_id=uuid4(),
        internet_message_id="message-1",
        from_email="owner@acme.test",
        to_emails=["z-buyer@customer.test", "a-buyer@vendor.test"],
        cc_emails=[],
        bcc_emails=[],
        has_attachments=False,
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=deal_id,
                integration_id=uuid4(),
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
                customer_domains=frozenset({"customer.test"}),
            )
        ]
    )
    service._associate_email = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "z-buyer@customer.test": contact,
            "a-buyer@vendor.test": contact,
            "unused@other.test": contact,
        },
        integration_id=None,
        attachment_scope_email_ids=set(),
    )

    assert result.stakeholder_candidates[0].contact_emails_by_id == {
        contact_id: "z-buyer@customer.test"
    }


@pytest.mark.asyncio
async def test_targeted_email_replay_adds_new_deal_despite_existing_association() -> None:
    """A historical link to another deal must not block a newly eligible target deal."""
    contact_id = uuid4()
    existing_deal_id = uuid4()
    target_deal_id = uuid4()
    email = SimpleNamespace(
        email_id=uuid4(),
        internet_message_id="message-1",
        from_email="owner@acme.test",
        to_emails=["buyer@customer.test"],
        cc_emails=[],
        bcc_emails=[],
        has_attachments=False,
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(  # type: ignore[method-assign]
        return_value=({"message-1": {existing_deal_id}}, {})
    )
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=target_deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
        ]
    )
    service._associate_email = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        target_deal_ids={target_deal_id},
        attachment_scope_email_ids={email.email_id},
    )

    assert result.associations_created == 1
    assert result.email_deal_ids == {target_deal_id}
    service._associate_email.assert_awaited_once_with(  # type: ignore[attr-defined]
        email,
        deal_id=target_deal_id,
        contact_ids={contact_id},
        contact_emails={"buyer@customer.test"},
    )


@pytest.mark.asyncio
async def test_targeted_replay_associates_only_matching_target_deal() -> None:
    """A deal-scoped replay may link its target without mutating another matching deal."""
    contact_id = uuid4()
    target_deal_id = uuid4()
    other_deal_id = uuid4()
    email = SimpleNamespace(
        email_id=uuid4(),
        internet_message_id="message-1",
        from_email="owner@acme.test",
        to_emails=["buyer@customer.test"],
        cc_emails=[],
        bcc_emails=[],
        has_attachments=True,
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=target_deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            ),
            DealCandidate(
                deal_id=other_deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            ),
        ]
    )
    service._associate_email = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        target_deal_ids={target_deal_id},
        attachment_scope_email_ids={email.email_id},
    )

    assert result.associations_created == 1
    assert result.ambiguous == 0
    assert result.email_deal_ids == {target_deal_id}
    service._associate_email.assert_awaited_once()  # type: ignore[attr-defined]
    assert (
        service._associate_email.await_args.kwargs["deal_id"]  # type: ignore[attr-defined]
        == target_deal_id
    )


@pytest.mark.asyncio
async def test_targeted_replay_never_writes_a_different_unique_deal() -> None:
    """Replaying Deal A may observe Deal B, but must not mutate Deal B."""
    contact_id = uuid4()
    other_deal_id = uuid4()
    email = SimpleNamespace(
        email_id=uuid4(),
        internet_message_id="message-1",
        from_email="owner@acme.test",
        to_emails=["buyer@customer.test"],
        cc_emails=[],
        bcc_emails=[],
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=other_deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
        ]
    )
    service._associate_email = AsyncMock()  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        target_deal_ids={uuid4()},
        attachment_scope_email_ids={email.email_id},
    )

    assert result.unmatched == 1
    assert result.email_deal_ids == set()
    assert result.attachment_email_ids_by_deal == {}
    service._associate_email.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_filtered_meeting_attendee_cannot_anchor_association() -> None:
    """A declined attendee removed by resolution must not re-enter deal matching."""
    contact_id = uuid4()
    event_id = uuid4()
    event = SimpleNamespace(
        event_id=event_id,
        status="confirmed",
        organiser_email="owner@acme.test",
        attendees=[SimpleNamespace(email="buyer@customer.test")],
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._cancel_event_associations = AsyncMock(return_value=0)  # type: ignore[method-assign]
    service._load_existing_deal_ids = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=uuid4(),
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
        ]
    )
    service._associate_event = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[],
        events=[event],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        event_contact_identities={event_id: frozenset()},
        integration_id=None,
        attachment_scope_email_ids=set(),
    )

    assert result.unmatched == 1
    service._associate_event.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_targeted_meeting_replay_adds_unique_deal_despite_stale_association() -> None:
    """A stale historical meeting link must not override one current unique match."""
    contact_id = uuid4()
    existing_deal_id = uuid4()
    target_deal_id = uuid4()
    event = SimpleNamespace(
        event_id=uuid4(),
        status="confirmed",
        organiser_email="owner@acme.test",
        attendees=[SimpleNamespace(email="buyer@customer.test")],
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(  # type: ignore[method-assign]
        return_value=({}, {event.event_id: {existing_deal_id}})
    )
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=target_deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
        ]
    )
    service._associate_event = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[],
        events=[event],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        target_deal_ids={target_deal_id},
        attachment_scope_email_ids=set(),
    )

    assert result.associations_created == 1
    assert result.ambiguous == 0
    service._associate_event.assert_awaited_once_with(  # type: ignore[attr-defined]
        event,
        deal_id=target_deal_id,
        contact_ids={contact_id},
        contact_emails={"buyer@customer.test"},
        integration_id=None,
    )


@pytest.mark.asyncio
async def test_stale_meeting_associations_without_current_match_are_unmatched() -> None:
    """Historical links alone must not report current deal ambiguity."""
    event = SimpleNamespace(
        event_id=uuid4(),
        status="confirmed",
        organiser_email="owner@acme.test",
        attendees=[SimpleNamespace(email="buyer@customer.test")],
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_existing_deal_ids = AsyncMock(  # type: ignore[method-assign]
        return_value=({}, {event.event_id: {uuid4(), uuid4()}})
    )
    service._load_deal_candidates = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[],
        events=[event],
        identity_contacts={},
        integration_id=None,
        attachment_scope_email_ids=set(),
    )

    assert result.unmatched == 1
    assert result.ambiguous == 0


@pytest.mark.asyncio
async def test_deal_candidates_use_only_open_approved_contacts_and_current_team(
    monkeypatch,
) -> None:
    """Pending contacts cannot anchor expansion, while active USER participants can anchor mail."""
    approved_contact_id = uuid4()
    pending_contact_id = uuid4()
    deal_id = uuid4()
    deal = SimpleNamespace(
        deal_id=deal_id,
        integration_id=uuid4(),
        owner_id=None,
        company_domain="customer.test",
        owner_email="owner@acme.test",
        participants=[
            SimpleNamespace(
                participant_type=CRMParticipantType.CONTACT,
                related_local_id=approved_contact_id,
                related_email="buyer@customer.test",
                approval_status="APPROVED",
                is_deleted=False,
            ),
            SimpleNamespace(
                participant_type=CRMParticipantType.CONTACT,
                related_local_id=pending_contact_id,
                related_email="pending@customer.test",
                approval_status="PENDING",
                is_deleted=False,
            ),
            SimpleNamespace(
                participant_type=CRMParticipantType.USER,
                related_local_id=uuid4(),
                related_email="seller@acme.test",
                approval_status="APPROVED",
                is_deleted=False,
            ),
        ],
    )
    scalars = MagicMock()
    scalars.unique.return_value.all.return_value = [deal]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session = MagicMock()
    session.execute = AsyncMock(return_value=execute_result)
    aliases_by_user = AsyncMock(return_value={})
    monkeypatch.setattr(
        association_module,
        "get_active_integration_emails_grouped_by_user",
        aliases_by_user,
    )

    candidates = await CommunicationDealAssociationService(
        session,
        uuid4(),
    )._load_deal_candidates(  # noqa: SLF001
        {approved_contact_id, pending_contact_id},
        deal_ids={deal_id},
    )

    assert candidates == [
        DealCandidate(
            deal_id=deal_id,
            contact_ids=frozenset({approved_contact_id}),
            internal_emails=frozenset({"owner@acme.test", "seller@acme.test"}),
            integration_id=deal.integration_id,
            customer_domains=frozenset({"customer.test"}),
        )
    ]
    aliases_by_user.assert_awaited_once()
    assert "crm_deals.deal_id IN" in str(session.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_deal_candidates_batch_expand_current_team_integration_aliases(
    monkeypatch,
) -> None:
    """Owner and current USER mailboxes qualify without expanding deleted participants."""
    approved_contact_id = uuid4()
    owner_id = uuid4()
    collaborator_id = uuid4()
    deleted_user_id = uuid4()
    first_deal_id = uuid4()
    second_deal_id = uuid4()
    first_deal = SimpleNamespace(
        deal_id=first_deal_id,
        integration_id=uuid4(),
        owner_id=owner_id,
        owner_email="owner@acme.test",
        company_domain="customer.test",
        participants=[
            SimpleNamespace(
                participant_type=CRMParticipantType.CONTACT,
                related_local_id=approved_contact_id,
                related_email="buyer@customer.test",
                approval_status="APPROVED",
                is_deleted=False,
            ),
            SimpleNamespace(
                participant_type=CRMParticipantType.USER,
                related_local_id=collaborator_id,
                related_email="seller@acme.test",
                approval_status="APPROVED",
                is_deleted=False,
            ),
            SimpleNamespace(
                participant_type=CRMParticipantType.USER,
                related_local_id=deleted_user_id,
                related_email="former@acme.test",
                approval_status="APPROVED",
                is_deleted=True,
            ),
        ],
    )
    second_deal = SimpleNamespace(
        deal_id=second_deal_id,
        integration_id=uuid4(),
        owner_id=collaborator_id,
        owner_email="seller@acme.test",
        company_domain="customer.test",
        participants=[
            SimpleNamespace(
                participant_type=CRMParticipantType.CONTACT,
                related_local_id=approved_contact_id,
                related_email="buyer@customer.test",
                approval_status="APPROVED",
                is_deleted=False,
            )
        ],
    )
    scalars = MagicMock()
    scalars.unique.return_value.all.return_value = [first_deal, second_deal]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session = MagicMock()
    session.execute = AsyncMock(return_value=execute_result)
    aliases_by_user = AsyncMock(
        return_value={
            owner_id: {"owner@gmail.test"},
            collaborator_id: {"seller@outlook.test"},
            deleted_user_id: {"former@gmail.test"},
        }
    )
    monkeypatch.setattr(
        association_module,
        "get_active_integration_emails_grouped_by_user",
        aliases_by_user,
        raising=False,
    )

    candidates = await CommunicationDealAssociationService(
        session,
        (organisation_id := uuid4()),
    )._load_deal_candidates({approved_contact_id})  # noqa: SLF001

    assert candidates[0].internal_emails == frozenset(
        {
            "owner@acme.test",
            "seller@acme.test",
            "owner@gmail.test",
            "seller@outlook.test",
        }
    )
    assert "former@gmail.test" not in candidates[0].internal_emails
    assert candidates[1].internal_emails == frozenset({"seller@acme.test", "seller@outlook.test"})
    aliases_by_user.assert_awaited_once()
    assert aliases_by_user.await_args.kwargs["organisation_id"] == organisation_id
    assert set(aliases_by_user.await_args.kwargs["created_by_user_ids"]) == {
        owner_id,
        collaborator_id,
    }


@pytest.mark.asyncio
async def test_existing_associations_are_loaded_for_only_changed_sources() -> None:
    """Hydration retries must preserve durable source associations before applying rules."""
    email_deal_id = uuid4()
    event_deal_id = uuid4()
    event_id = uuid4()
    email_result = MagicMock()
    email_result.scalars.return_value.all.return_value = [
        SimpleNamespace(internet_message_id="message-1", deal_id=email_deal_id)
    ]
    event_result = MagicMock()
    event_result.scalars.return_value.all.return_value = [
        SimpleNamespace(event_id=event_id, deal_id=event_deal_id)
    ]
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[email_result, event_result])

    email_deals, event_deals = await CommunicationDealAssociationService(
        session,
        uuid4(),
    )._load_existing_deal_ids(  # noqa: SLF001
        [SimpleNamespace(internet_message_id="message-1")],
        [SimpleNamespace(event_id=event_id)],
        deal_ids={email_deal_id, event_deal_id},
    )

    assert email_deals == {"message-1": {email_deal_id}}
    assert event_deals == {event_id: {event_deal_id}}
    assert session.execute.await_count == 2
    statements = [str(execute_call.args[0]) for execute_call in session.execute.await_args_list]
    assert all("deal_id IN" in statement for statement in statements)


@pytest.mark.asyncio
async def test_email_association_is_idempotent_and_projects_first_insert(monkeypatch) -> None:
    """A hydration retry must not duplicate the source association or its feed card."""
    execute_result = SimpleNamespace(
        rowcount=1,
        scalar_one_or_none=lambda: uuid4(),
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=execute_result)
    projection = MagicMock()
    projection.upsert_email_association = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "ActivityFeedProjectionService",
        lambda *_args: projection,
        raising=False,
    )
    email = SimpleNamespace(
        internet_message_id="message-1",
        from_email="buyer@customer.test",
        subject="Pricing",
        received_at=None,
        sent_at=None,
    )
    service = CommunicationDealAssociationService(
        session,
        uuid4(),
    )
    update_last_contact = AsyncMock()
    service._update_last_contact = update_last_contact  # type: ignore[method-assign]

    created = await service._associate_email(  # noqa: SLF001
        email,
        deal_id=(deal_id := uuid4()),
        contact_ids=(contact_ids := {uuid4()}),
        contact_emails={"buyer@customer.test"},
    )

    assert created is True
    projected_association = projection.upsert_email_association.await_args.args[0]
    assert projected_association.internet_message_id == "message-1"
    assert projected_association.matched_stakeholder_emails == ["buyer@customer.test"]
    projection.upsert_email_association.assert_awaited_once_with(
        projected_association,
        email,
        refresh_deal_activity=False,
    )
    update_last_contact.assert_awaited_once_with(
        deal_id=deal_id,
        contact_ids=contact_ids,
        contact_time=None,
    )


@pytest.mark.asyncio
async def test_duplicate_email_association_has_no_projection_side_effect(monkeypatch) -> None:
    """Only the worker that wins ON CONFLICT may replace the direct activity card."""
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(
            rowcount=0,
            scalar_one_or_none=lambda: None,
        )
    )
    projection = MagicMock()
    projection.upsert_email_association = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "ActivityFeedProjectionService",
        lambda *_args: projection,
        raising=False,
    )

    created = await CommunicationDealAssociationService(
        session,
        uuid4(),
    )._associate_email(  # noqa: SLF001
        SimpleNamespace(
            internet_message_id="message-1",
            from_email="buyer@customer.test",
            subject=None,
            received_at=None,
            sent_at=None,
        ),
        deal_id=uuid4(),
        contact_ids={uuid4()},
        contact_emails={"buyer@customer.test"},
    )

    assert created is False
    projection.upsert_email_association.assert_not_awaited()


@pytest.mark.asyncio
async def test_removed_email_association_reactivates_with_persisted_id(monkeypatch) -> None:
    """A rematch must atomically reactivate the unique row and reuse its feed identity."""
    persisted_association_id = uuid4()
    statements: list[object] = []

    async def execute(statement):
        statements.append(statement)
        return SimpleNamespace(
            rowcount=1,
            scalar_one_or_none=lambda: persisted_association_id,
        )

    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute)
    projection = MagicMock()
    projection.upsert_email_association = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "ActivityFeedProjectionService",
        lambda *_args: projection,
    )
    service = CommunicationDealAssociationService(session, uuid4())
    service._update_last_contact = AsyncMock()  # type: ignore[method-assign]
    email = SimpleNamespace(
        internet_message_id="message-1",
        from_email="buyer@customer.test",
        subject=None,
        received_at=None,
        sent_at=None,
    )

    created = await service._associate_email(  # noqa: SLF001
        email,
        deal_id=uuid4(),
        contact_ids={uuid4()},
        contact_emails={"buyer@customer.test"},
    )

    assert created is True
    projected = projection.upsert_email_association.await_args.args[0]
    assert projected.association_id == persisted_association_id
    sql = str(statements[0])
    assert "DO UPDATE" in sql
    assert "status" in sql
    assert "RETURNING" in sql


@pytest.mark.asyncio
async def test_meeting_association_is_idempotent_and_projects_first_insert(monkeypatch) -> None:
    """Calendar hydration must create the compatibility association exactly once."""
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(
            rowcount=1,
            scalar_one_or_none=lambda: uuid4(),
        )
    )
    projection = MagicMock()
    projection.upsert_meeting_association = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "ActivityFeedProjectionService",
        lambda *_args: projection,
    )
    integration_id = uuid4()
    event = SimpleNamespace(event_id=uuid4(), end_at=None, start_at=None)

    service = CommunicationDealAssociationService(
        session,
        uuid4(),
    )
    update_last_contact = AsyncMock()
    service._update_last_contact = update_last_contact  # type: ignore[method-assign]

    created = await service._associate_event(  # noqa: SLF001
        event,
        deal_id=(deal_id := uuid4()),
        contact_ids=(contact_ids := {uuid4()}),
        contact_emails={"buyer@customer.test"},
        integration_id=integration_id,
    )

    assert created is True
    association = projection.upsert_meeting_association.await_args.args[0]
    assert association.event_id == event.event_id
    projection.upsert_meeting_association.assert_awaited_once_with(
        association,
        event,
        integration_id=integration_id,
        refresh_deal_activity=False,
    )
    update_last_contact.assert_awaited_once_with(
        deal_id=deal_id,
        contact_ids=contact_ids,
        contact_time=None,
    )


@pytest.mark.asyncio
async def test_cancelled_event_association_reactivates_with_persisted_id(monkeypatch) -> None:
    """A valid rematch restores the unique meeting row without changing feed identity."""
    persisted_association_id = uuid4()
    statements: list[object] = []

    async def execute(statement):
        statements.append(statement)
        return SimpleNamespace(
            rowcount=1,
            scalar_one_or_none=lambda: persisted_association_id,
        )

    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute)
    projection = MagicMock()
    projection.upsert_meeting_association = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "ActivityFeedProjectionService",
        lambda *_args: projection,
    )
    service = CommunicationDealAssociationService(session, uuid4())
    service._update_last_contact = AsyncMock()  # type: ignore[method-assign]
    event = SimpleNamespace(event_id=uuid4(), end_at=None, start_at=None)

    created = await service._associate_event(  # noqa: SLF001
        event,
        deal_id=uuid4(),
        contact_ids={uuid4()},
        contact_emails={"buyer@customer.test"},
        integration_id=uuid4(),
    )

    assert created is True
    projected = projection.upsert_meeting_association.await_args.args[0]
    assert projected.association_id == persisted_association_id
    sql = str(statements[0])
    assert "DO UPDATE" in sql
    assert "status" in sql
    assert "RETURNING" in sql


@pytest.mark.asyncio
async def test_changed_cancelled_meeting_deactivates_existing_association() -> None:
    """A provider cancellation must hide an already-linked deal meeting immediately."""
    event_id = uuid4()
    event = SimpleNamespace(
        event_id=event_id,
        status="cancelled",
        organiser_email="seller@acme.test",
        attendees=[],
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._cancel_event_associations = AsyncMock(return_value=1)  # type: ignore[method-assign]
    service._load_existing_deal_ids = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    service._load_deal_candidates = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[],
        events=[event],
        identity_contacts={},
        integration_id=None,
        attachment_scope_email_ids=set(),
    )

    service._cancel_event_associations.assert_awaited_once_with({event_id}, deal_ids=None)
    assert result.associations_deactivated == 1


@pytest.mark.asyncio
async def test_targeted_cancelled_meeting_deactivates_only_replayed_deal(
    monkeypatch,
) -> None:
    """Deal replay must not cancel the same meeting's link on another deal."""
    event_id = uuid4()
    target_deal_id = uuid4()
    other_deal_id = uuid4()
    target_association = SimpleNamespace(
        association_id=uuid4(),
        deal_id=target_deal_id,
        status=association_module.AssociationStatus.ACTIVE,
        updated_at=None,
    )
    other_association = SimpleNamespace(
        association_id=uuid4(),
        deal_id=other_deal_id,
        status=association_module.AssociationStatus.ACTIVE,
        updated_at=None,
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [target_association]
    session = MagicMock()

    async def execute_scoped(statement):
        deal_filter = next(
            value for key, value in statement.compile().params.items() if key.startswith("deal_id_")
        )
        assert set(deal_filter) == {target_deal_id}
        return result

    session.execute = AsyncMock(side_effect=execute_scoped)
    projection = MagicMock()
    projection.soft_delete_activity_projection = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "ActivityFeedProjectionService",
        lambda *_args: projection,
    )
    service = CommunicationDealAssociationService(session, uuid4())

    cancelled = await service._cancel_event_associations(  # noqa: SLF001
        {event_id},
        deal_ids={target_deal_id},
    )

    assert cancelled == 1
    assert target_association.status == association_module.AssociationStatus.CANCELLED
    assert other_association.status == association_module.AssociationStatus.ACTIVE
    projection.soft_delete_activity_projection.assert_awaited_once_with(
        target_association.association_id
    )


@pytest.mark.asyncio
async def test_deal_replay_removes_association_after_contact_participant_removal(
    monkeypatch,
) -> None:
    """Deal-scoped replay must hide links that no longer have an approved contact anchor."""
    deal_id = uuid4()
    association = SimpleNamespace(
        association_id=uuid4(),
        deal_id=deal_id,
        status="ACTIVE",
        updated_at=None,
    )
    email = SimpleNamespace(
        email_id=uuid4(),
        integration_id=uuid4(),
        thread_id=None,
        from_email="buyer@customer.com",
        from_name=None,
        to_emails=["seller@acme.com"],
        cc_emails=[],
        bcc_emails=[],
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_associated_communications = AsyncMock(  # type: ignore[method-assign]
        return_value=([(association, email)], [])
    )
    service._load_deal_candidates = AsyncMock(return_value=[])  # type: ignore[method-assign]
    contact_writer = MagicMock()
    contact_writer.find_contacts_by_email_batch = AsyncMock(
        return_value={
            "buyer@customer.com": SimpleNamespace(contact_id=uuid4()),
        }
    )
    projection = MagicMock()
    projection.soft_delete_activity_projection = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "ContactWriteService",
        lambda *_args: contact_writer,
    )
    monkeypatch.setattr(
        association_module,
        "ActivityFeedProjectionService",
        lambda *_args: projection,
    )

    removed = await service.reconcile_deal_associations(
        deal_ids=[deal_id],
        context=ContactCommunicationHydrationContext(
            internal_emails={"seller@acme.com"},
            internal_domains={"acme.com"},
        ),
    )

    assert removed == 1
    assert association.status == association_module.AssociationStatus.REMOVED
    projection.soft_delete_activity_projection.assert_awaited_once_with(association.association_id)


@pytest.mark.asyncio
async def test_deal_replay_removes_meeting_anchored_only_by_declined_attendee(
    monkeypatch,
) -> None:
    """Declined attendees cannot preserve an old meeting-to-deal association."""
    deal_id = uuid4()
    contact_id = uuid4()
    association = SimpleNamespace(
        association_id=uuid4(),
        deal_id=deal_id,
        status="ACTIVE",
        updated_at=None,
    )
    event = SimpleNamespace(
        event_id=uuid4(),
        status="confirmed",
        organiser_email="seller@acme.com",
        organiser_name=None,
        attendees=[
            SimpleNamespace(
                email="buyer@customer.com",
                display_name="Buyer",
                response_status="declined",
                data_payload={},
            )
        ],
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_associated_communications = AsyncMock(  # type: ignore[method-assign]
        return_value=([], [(association, event)])
    )
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"seller@acme.com"}),
            )
        ]
    )
    contact_writer = MagicMock()
    contact_writer.find_contacts_by_email_batch = AsyncMock(return_value={})
    projection = MagicMock()
    projection.soft_delete_activity_projection = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "ContactWriteService",
        lambda *_args: contact_writer,
    )
    monkeypatch.setattr(
        association_module,
        "ActivityFeedProjectionService",
        lambda *_args: projection,
    )

    removed = await service.reconcile_deal_associations(
        deal_ids=[deal_id],
        context=ContactCommunicationHydrationContext(
            internal_emails={"seller@acme.com"},
            internal_domains={"acme.com"},
        ),
    )

    assert removed == 1
    assert association.status == association_module.AssociationStatus.REMOVED
    contact_writer.find_contacts_by_email_batch.assert_awaited_once_with([])
    service._load_deal_candidates.assert_awaited_once_with(  # type: ignore[attr-defined]
        set(),
        deal_ids={deal_id},
    )


@pytest.mark.asyncio
async def test_deal_replay_keeps_meeting_that_matches_multiple_deals(
    monkeypatch,
) -> None:
    """Replay must preserve each meeting link that still satisfies participant policy."""
    target_deal_id = uuid4()
    other_deal_id = uuid4()
    contact_id = uuid4()
    association = SimpleNamespace(
        association_id=uuid4(),
        deal_id=target_deal_id,
        status="ACTIVE",
        updated_at=None,
    )
    event = SimpleNamespace(
        event_id=uuid4(),
        status="confirmed",
        organiser_email="seller@acme.com",
        organiser_name=None,
        attendees=[
            SimpleNamespace(
                email="buyer@customer.com",
                display_name="Buyer",
                response_status="accepted",
                data_payload={},
            )
        ],
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_associated_communications = AsyncMock(  # type: ignore[method-assign]
        return_value=([], [(association, event)])
    )
    service._load_deal_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            DealCandidate(
                deal_id=deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"seller@acme.com"}),
            )
            for deal_id in (target_deal_id, other_deal_id)
        ]
    )
    contact_writer = MagicMock()
    contact_writer.find_contacts_by_email_batch = AsyncMock(
        return_value={
            "buyer@customer.com": SimpleNamespace(contact_id=contact_id),
        }
    )
    projection = MagicMock()
    projection.soft_delete_activity_projection = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "ContactWriteService",
        lambda *_args: contact_writer,
    )
    monkeypatch.setattr(
        association_module,
        "ActivityFeedProjectionService",
        lambda *_args: projection,
    )

    removed = await service.reconcile_deal_associations(
        deal_ids=[target_deal_id],
        context=ContactCommunicationHydrationContext(
            internal_emails={"seller@acme.com"},
            internal_domains={"acme.com"},
        ),
    )

    assert removed == 0
    assert association.status == association_module.AssociationStatus.ACTIVE
    projection.soft_delete_activity_projection.assert_not_awaited()
    contact_writer.find_contacts_by_email_batch.assert_awaited_once_with(["buyer@customer.com"])
