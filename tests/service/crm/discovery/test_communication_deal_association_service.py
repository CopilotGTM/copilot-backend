"""Business-rule tests for communication-to-deal association."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import src.service.crm.discovery.communication_deal_association as association_module
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType
from src.service.crm.discovery.communication_deal_association import (
    CommunicationCandidate,
    CommunicationDealAssociationService,
    DealCandidate,
    communication_matches_deal,
    resolve_unique_deal_id,
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
    service._discover_participants = AsyncMock(return_value=set())  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
    )

    assert result.associations_created == 1
    assert result.ambiguous == 0
    assert result.email_deal_ids == {deal_id}
    assert result.attachment_deal_ids == ({deal_id} if expects_attachment_deal else set())
    service._associate_email.assert_awaited_once_with(  # type: ignore[attr-defined]
        email,
        deal_id=deal_id,
        contact_ids={contact_id},
        contact_emails={"buyer@customer.test"},
    )


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
    )

    assert result.email_deal_ids == {deal_id}
    assert result.attachment_deal_ids == {deal_id}
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
    )

    assert result.unmatched == 1
    assert result.email_deal_ids == set()
    assert result.attachment_deal_ids == set()
    service._update_last_contact.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_changed_email_associates_every_matching_deal() -> None:
    """One email may be valid activity for multiple deals sharing its participants."""
    contact_id = uuid4()
    deal_ids = {uuid4(), uuid4()}
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
                deal_id=deal_id,
                contact_ids=frozenset({contact_id}),
                internal_emails=frozenset({"owner@acme.test"}),
            )
            for deal_id in deal_ids
        ]
    )
    service._associate_email = AsyncMock(return_value=True)  # type: ignore[method-assign]
    service._associate_event = AsyncMock()  # type: ignore[method-assign]
    service._discover_participants = AsyncMock(return_value=set())  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
    )

    assert result.associations_created == 2
    assert result.ambiguous == 0
    assert result.email_deal_ids == deal_ids
    assert result.attachment_deal_ids == deal_ids
    assert {
        call.kwargs["deal_id"]
        for call in service._associate_email.await_args_list  # type: ignore[attr-defined]
    } == deal_ids


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
    service._discover_participants = AsyncMock(return_value=set())  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        target_deal_ids={target_deal_id},
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
    service._discover_participants = AsyncMock(return_value=set())  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[email],
        events=[],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        target_deal_ids={target_deal_id},
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
    )

    assert result.unmatched == 1
    assert result.email_deal_ids == set()
    assert result.attachment_deal_ids == set()
    service._associate_email.assert_not_awaited()  # type: ignore[attr-defined]


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
    service._discover_participants = AsyncMock(return_value=set())  # type: ignore[method-assign]

    result = await service.associate_changed_communications(
        emails=[],
        events=[event],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=contact_id),
        },
        integration_id=None,
        target_deal_ids={target_deal_id},
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
    )._load_deal_candidates({approved_contact_id, pending_contact_id})  # noqa: SLF001

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
    )

    assert email_deals == {"message-1": {email_deal_id}}
    assert event_deals == {event_id: {event_deal_id}}
    assert session.execute.await_count == 2


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
    projection.upsert_email_association.assert_awaited_once_with(projected_association, email)
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
async def test_participant_discovery_is_capped_and_returns_only_approved_contacts() -> None:
    """The sixth discovered contact remains pending and cannot widen history replay."""
    contact_ids = {uuid4() for _ in range(6)}
    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[MagicMock(), existing_result])
    session.flush = AsyncMock()
    integration_id = uuid4()
    service = CommunicationDealAssociationService(session, uuid4())

    approved = await service._discover_participants(  # noqa: SLF001
        deal=DealCandidate(
            deal_id=uuid4(),
            contact_ids=frozenset(),
            internal_emails=frozenset(),
            integration_id=integration_id,
            customer_domains=frozenset({"customer.test"}),
        ),
        contact_emails_by_id={
            contact_id: f"{index}@customer.test"
            for index, contact_id in enumerate(sorted(contact_ids, key=str))
        },
        contact_ids=contact_ids,
        source=association_module.StakeholderSource.EMAIL_DISCOVERY,
    )

    participants = [call.args[0] for call in session.add.call_args_list]
    assert len(participants) == 6
    assert sum(row.approval_status == "APPROVED" for row in participants) == 5
    assert sum(row.approval_status == "PENDING" for row in participants) == 1
    assert approved == {
        row.related_local_id for row in participants if row.approval_status == "APPROVED"
    }


@pytest.mark.asyncio
async def test_participant_discovery_rejects_unrelated_external_domains() -> None:
    """A vendor copied on a customer thread must not become a deal participant."""
    customer_id = uuid4()
    vendor_id = uuid4()
    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[MagicMock(), existing_result])
    session.flush = AsyncMock()
    service = CommunicationDealAssociationService(session, uuid4())

    approved = await service._discover_participants(  # noqa: SLF001
        deal=DealCandidate(
            deal_id=uuid4(),
            contact_ids=frozenset(),
            internal_emails=frozenset(),
            integration_id=uuid4(),
            customer_domains=frozenset({"customer.test"}),
        ),
        contact_emails_by_id={
            customer_id: "new@customer.test",
            vendor_id: "lawyer@vendor.test",
        },
        contact_ids={customer_id, vendor_id},
        source=association_module.StakeholderSource.EMAIL_DISCOVERY,
    )

    assert approved == {customer_id}
    assert [call.args[0].related_local_id for call in session.add.call_args_list] == [customer_id]


@pytest.mark.asyncio
async def test_participant_discovery_replays_research_for_existing_approved_contact() -> None:
    """A task retry must retain the post-commit research signal after the row already exists."""
    contact_id = uuid4()
    existing = SimpleNamespace(
        related_local_id=contact_id,
        participant_source=association_module.StakeholderSource.EMAIL_DISCOVERY.value,
        approval_status=association_module.ApprovalStatus.APPROVED.value,
        is_deleted=False,
    )
    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = [existing]
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[MagicMock(), existing_result])
    service = CommunicationDealAssociationService(session, uuid4())

    approved = await service._discover_participants(  # noqa: SLF001
        deal=DealCandidate(
            deal_id=uuid4(),
            contact_ids=frozenset(),
            internal_emails=frozenset(),
            integration_id=uuid4(),
            customer_domains=frozenset({"customer.test"}),
        ),
        contact_emails_by_id={contact_id: "buyer@customer.test"},
        contact_ids={contact_id},
        source=association_module.StakeholderSource.EMAIL_DISCOVERY,
    )

    assert approved == {contact_id}
    session.add.assert_not_called()


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
    )

    service._cancel_event_associations.assert_awaited_once_with({event_id})
    assert result.associations_deactivated == 1


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
        from_email="buyer@customer.test",
        to_emails=["seller@acme.test"],
        cc_emails=[],
        bcc_emails=[],
    )
    service = CommunicationDealAssociationService(MagicMock(), uuid4())
    service._load_associated_communications = AsyncMock(  # type: ignore[method-assign]
        return_value=([(association, email)], [])
    )
    service._load_deal_candidates = AsyncMock(return_value=[])  # type: ignore[method-assign]
    projection = MagicMock()
    projection.soft_delete_activity_projection = AsyncMock()
    monkeypatch.setattr(
        association_module,
        "ActivityFeedProjectionService",
        lambda *_args: projection,
    )

    removed = await service.reconcile_deal_associations(
        deal_ids=[deal_id],
        identity_contacts={
            "buyer@customer.test": SimpleNamespace(contact_id=uuid4()),
        },
    )

    assert removed == 1
    assert association.status == association_module.AssociationStatus.REMOVED
    projection.soft_delete_activity_projection.assert_awaited_once_with(association.association_id)
