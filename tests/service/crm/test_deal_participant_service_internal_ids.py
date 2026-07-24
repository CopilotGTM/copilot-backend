"""Regression tests for internal CRM participant writer boundary rules."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.crm.participants.contracts import ParticipantWriteResult
from src.service.crm.participants.participant_write_service import ParticipantWriteService


class _FakeResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def scalars(self) -> _FakeResult:
        """Return self to mimic SQLAlchemy scalar result chaining."""
        return self

    def first(self) -> object | None:
        """Return the first configured fake row, matching scalar result behavior."""
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, rows: list[object] | None = None):
        self._rows = rows or []
        self._added: list[object] = []
        self._objects: dict[object, object] = {}

    async def execute(self, _stmt):
        """Return configured rows without evaluating SQL expressions."""
        return _FakeResult(self._rows)

    async def get(self, _model, obj_id):
        """Look up fake ORM instances by primary key."""
        return self._objects.get(obj_id)

    def add(self, obj: object) -> None:
        """Capture added or mutated ORM instances for assertions."""
        self._added.append(obj)


def test_contact_owned_profile_fields_are_not_mapped_on_participant() -> None:
    """Participant rows should not expose contact-owned profile/research columns."""
    removed_fields = {
        "job_title",
        "department",
        "phone_number",
        "linkedin_url",
        "external_links",
        "stakeholder_research",
        "last_researched_at",
    }

    assert removed_fields.isdisjoint(CRMDealParticipant.model_fields)
    assert removed_fields.isdisjoint(CRMDealParticipant.__table__.columns.keys())


@pytest.mark.asyncio
async def test_maybe_apply_company_to_deal_sets_internal_company_id() -> None:
    """Company participants update the internal deal company snapshot."""
    session = _FakeSession()
    organisation_id = uuid4()
    company_id = uuid4()
    integration_id = uuid4()
    company = SimpleNamespace(
        company_id=company_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        name="Acme",
        domain="acme.com",
        is_deleted=False,
    )
    session._objects[company_id] = company

    deal = SimpleNamespace(
        company_id=None,
        company_name=None,
        company_domain=None,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )

    service = ParticipantWriteService(
        session=session,
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
    )

    now = datetime.now(UTC)
    await service._maybe_apply_company_to_deal(
        deal=deal,
        integration_id=integration_id,
        related_local_id=company_id,
        updated_at=now,
    )

    assert deal.company_id == company_id
    assert deal.company_name == "Acme"
    assert deal.company_domain == "acme.com"


@pytest.mark.asyncio
async def test_add_participants_reuses_contact_when_same_contact_has_multiple_roles() -> None:
    """Contact participants dedupe by person while preserving primary-contact state."""
    session = _FakeSession()
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()

    service = ParticipantWriteService(
        session=session,
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
    )
    service._get_internal_deal = AsyncMock(  # type: ignore[method-assign]
        return_value=(SimpleNamespace(deal_id=deal_id), integration_id)
    )
    service._resolve_participant_entity = AsyncMock(  # type: ignore[method-assign]
        return_value=(contact_id, "Ada Lovelace", "ada@example.com")
    )
    service._find_existing_participant = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.add_participants(
        deal_id,
        [
            {
                "participant_type": CRMParticipantType.CONTACT.value,
                "related_id": contact_id,
                "relationship_role": "decision_maker",
            },
            {
                "participant_type": CRMParticipantType.CONTACT.value,
                "related_id": contact_id,
                "relationship_role": "primary_contact",
            },
        ],
    )

    created = result.created
    skipped = result.skipped
    changed = result.contact_participant_changed
    assert len(created) == 1
    assert len(skipped) == 1
    assert skipped[0].reason == "Duplicate participant in request"
    assert changed is True
    assert isinstance(created[0], CRMDealParticipant)
    assert created[0].related_name is None
    assert created[0].is_primary_contact is True
    assert created[0].relationship_role == "decision_maker"
    assert getattr(created[0], "properties", None) is None


@pytest.mark.asyncio
async def test_participant_write_service_returns_typed_result() -> None:
    """Participant writes return counters and rows through the canonical result contract."""
    session = _FakeSession()
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()

    service = ParticipantWriteService(
        session=session,
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
    )
    service._get_internal_deal = AsyncMock(  # type: ignore[method-assign]
        return_value=(SimpleNamespace(deal_id=deal_id), integration_id)
    )
    service._resolve_participant_entity = AsyncMock(  # type: ignore[method-assign]
        return_value=(contact_id, "Ada Lovelace", "ada@example.com")
    )
    service._find_existing_participant = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.add_participants(
        deal_id,
        [
            {
                "participant_type": CRMParticipantType.CONTACT.value,
                "related_id": contact_id,
                "relationship_role": "decision_maker",
            },
            {
                "participant_type": CRMParticipantType.CONTACT.value,
                "related_id": contact_id,
                "relationship_role": "primary_contact",
            },
        ],
    )

    assert isinstance(result, ParticipantWriteResult)
    assert len(result.created) == 1
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "Duplicate participant in request"
    assert result.contact_participant_changed is True
    assert result.integration_id == integration_id
    assert result.created[0].is_primary_contact is True


@pytest.mark.asyncio
async def test_legacy_primary_contact_role_is_stored_as_flag_only() -> None:
    session = _FakeSession()
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    service = ParticipantWriteService(
        session=session,
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
    )
    service._get_internal_deal = AsyncMock(
        return_value=(SimpleNamespace(deal_id=deal_id), integration_id)
    )
    service._resolve_participant_entity = AsyncMock(
        return_value=(contact_id, "Ada Lovelace", "ada@example.com")
    )
    service._find_existing_participant = AsyncMock(return_value=None)

    result = await service.add_participants(
        deal_id,
        [
            {
                "participant_type": CRMParticipantType.CONTACT.value,
                "related_id": contact_id,
                "relationship_role": "primary_contact",
            }
        ],
    )

    assert result.created[0].is_primary_contact is True
    assert result.created[0].relationship_role is None


@pytest.mark.asyncio
async def test_update_participant_role_to_owner_does_not_mutate_deal_owner_snapshot() -> None:
    """Raw participant role changes must not become authoritative owner assignment."""
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    existing_owner_id = uuid4()
    participant_user_id = uuid4()
    deal = SimpleNamespace(
        deal_id=deal_id,
        owner_id=existing_owner_id,
        owner_name="Existing Owner",
        owner_email="existing@example.com",
    )
    participant = CRMDealParticipant(
        participant_id=participant_id,
        organisation_id=organisation_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_type=CRMParticipantType.USER,
        related_local_id=participant_user_id,
        relationship_role="collaborator",
        is_deleted=False,
    )
    session = _FakeSession()
    session._objects[participant_id] = participant
    service = ParticipantWriteService(
        session=session,
        organisation_id=organisation_id,
        actor_user_id=uuid4(),
    )
    service._get_internal_deal = AsyncMock(  # type: ignore[method-assign]
        return_value=(deal, integration_id)
    )
    service._find_existing_participant = AsyncMock(return_value=None)  # type: ignore[method-assign]

    updated = await service.update_participant_role(deal_id, participant_id, "owner")

    assert updated.relationship_role == "owner"
    assert deal.owner_id == existing_owner_id
    assert deal.owner_name == "Existing Owner"
    assert deal.owner_email == "existing@example.com"
