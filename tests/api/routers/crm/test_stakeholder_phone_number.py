"""Tests for phone_number field across the stakeholder feature.

Covers:
- API handler: list, detail, create, update endpoints return/accept phone_number
- Service layer: create and update handle phone_number correctly
- Sync service: enriches phone_number from CRMContact and respects manual overrides
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from src.api.schemas.crm_models import UpdateStakeholderRequest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.crm.participants.stakeholder_read_service import StakeholderReadService
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService
from src.workers.deal_worker.services.deal_mapper_services.stakeholder_sync_service import (
    StakeholderSyncService,
)

from tests.api.routers.crm.conftest import FakeStakeholder, create_stakeholder


class TestPhoneNumberInListEndpoint:
    """GET /{deal_id}/stakeholders returns phone_number."""

    def test_phone_number_returned_in_stakeholder_list(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="contact@acme.com",
            phone_number="+1 (555) 123-4567",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders")

        assert resp.status_code == 200
        data = resp.json()["data"]["stakeholders"][0]
        assert data["phone_number"] == "+1 (555) 123-4567"

    def test_phone_number_null_when_not_set(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="nophone@acme.com",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders")

        assert resp.status_code == 200
        data = resp.json()["data"]["stakeholders"][0]
        assert data["phone_number"] is None


class TestPhoneNumberInDetailEndpoint:
    """GET /{deal_id}/stakeholders/{id} returns phone_number."""

    def test_phone_number_returned_in_stakeholder_detail(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        stakeholder = FakeStakeholder(
            stakeholder_id=uuid4(),
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="detail@acme.com",
            name="Detail Person",
            phone_number="+44 20 7946 0958",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}"
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["phone_number"] == "+44 20 7946 0958"

    def test_phone_number_null_in_detail_when_not_set(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        stakeholder = FakeStakeholder(
            stakeholder_id=uuid4(),
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="nophone-detail@acme.com",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}"
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["phone_number"] is None


class TestPhoneNumberInUpdateEndpoint:
    """PATCH /{deal_id}/stakeholders/{id} accepts phone_number."""

    def test_update_phone_number(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="update-phone@acme.com",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.patch(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}",
            json={"phone_number": "+91 9876543210"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"

        updated = fake_stakeholders_service._get_stakeholder(
            test_deal_id, stakeholder.stakeholder_id
        )
        assert updated is not None
        assert updated.phone_number == "+91 9876543210"
        assert "phone_number" in updated.stakeholder_data["manual_overrides"]

    def test_update_phone_number_to_null(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Phone number is a nullable field — setting it to null should work."""
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="clear-phone@acme.com",
            phone_number="+1 555-0100",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.patch(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}",
            json={"phone_number": None},
        )

        assert resp.status_code == 200
        updated = fake_stakeholders_service._get_stakeholder(
            test_deal_id, stakeholder.stakeholder_id
        )
        assert updated is not None
        assert updated.phone_number is None


def _write_service() -> StakeholderWriteService:
    session = AsyncMock()
    return StakeholderWriteService(session, uuid4())


def _read_service() -> StakeholderReadService:
    session = AsyncMock()
    return StakeholderReadService(session, uuid4())


@pytest.mark.asyncio
async def test_update_stakeholder_applies_phone_number_and_tracks_override() -> None:
    service = _write_service()
    contact_id = uuid4()
    integration_id = uuid4()
    participant = CRMDealParticipant(
        participant_id=uuid4(),
        deal_id=uuid4(),
        organisation_id=uuid4(),
        integration_id=integration_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_email="person@example.com",
        related_name="Test",
        stance="UNKNOWN",
        decision_power="UNKNOWN",
        temperature="UNKNOWN",
        participant_source="MANUAL",
        approval_status="APPROVED",
    )
    contact = CRMContact(
        contact_id=contact_id,
        organisation_id=participant.organisation_id,
        integration_id=integration_id,
        email="person@example.com",
    )
    service.get_stakeholder = AsyncMock(return_value=participant)  # type: ignore[method-assign]
    service._get_participant_contact = AsyncMock(return_value=contact)  # type: ignore[method-assign]

    request = UpdateStakeholderRequest(phone_number="+1 555-9999")
    result = await service.update_stakeholder(uuid4(), participant.participant_id, request)

    assert result.phone_number == "+1 555-9999"
    assert contact.phone == "+1 555-9999"
    assert not hasattr(participant, "phone_number")
    service.session.flush.assert_awaited()


@pytest.mark.asyncio
async def test_get_stakeholder_detail_includes_phone_number() -> None:
    service = _read_service()
    participant = SimpleNamespace(
        participant_id=uuid4(),
        deal_id=uuid4(),
        related_local_id=None,
        related_email="detail@example.com",
        related_name="Detail",
        job_title="CTO",
        phone_number="+49 30 12345678",
        department="Engineering",
        external_links=None,
        stance="CHAMPION",
        decision_power="DECISION_MAKER",
        motivation="Scale infra",
        pain_points=["reliability"],
        keywords=["cloud"],
        last_contact_at=None,
        temperature="HOT",
        source="CRM",
        participant_source="CRM",
        source_reference_id=None,
        approval_status="APPROVED",
        stakeholder_research=None,
        last_researched_at=None,
        is_deleted=False,
        created_at=None,
        updated_at=None,
    )
    result_mock = SimpleNamespace(scalar_one_or_none=lambda: participant)
    service.session.execute = AsyncMock(return_value=result_mock)

    result = await service.get_stakeholder_detail(participant.deal_id, participant.participant_id)

    assert result is not None
    assert result["phone_number"] == "+49 30 12345678"


@pytest.mark.asyncio
async def test_enrich_from_contact_links_contact_without_phone_snapshot() -> None:
    """Sync service should link CRMContact without copying phone to stakeholder."""
    service = StakeholderSyncService(uuid4())
    contact = SimpleNamespace(
        contact_id=uuid4(),
        full_name="CRM Contact",
        job_title="VP Sales",
        phone="+1 800-555-0123",
    )

    scalars_result = MagicMock()
    scalars_result.first.return_value = contact
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

    stakeholder = SimpleNamespace(
        contact_id=None,
        name="Existing Name",
        job_title="Existing Title",
        phone_number=None,
    )

    changed = await service._enrich_from_contact(  # noqa: SLF001
        session=session,
        stakeholder=stakeholder,
        email="person@example.com",
    )

    assert changed is True
    assert stakeholder.contact_id == contact.contact_id
    assert stakeholder.phone_number is None


@pytest.mark.asyncio
async def test_enrich_from_contact_respects_phone_manual_override() -> None:
    """If user manually set phone_number, sync must not overwrite it."""
    service = StakeholderSyncService(uuid4())
    contact = SimpleNamespace(
        contact_id=uuid4(),
        full_name=None,
        job_title=None,
        phone="+1 800-NEW-NUMBER",
    )

    scalars_result = MagicMock()
    scalars_result.first.return_value = contact
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

    stakeholder = SimpleNamespace(
        contact_id=contact.contact_id,
        name="Manual Name",
        job_title="Manual Title",
        phone_number="+1 555-MANUAL",
    )

    await service._enrich_from_contact(  # noqa: SLF001
        session=session,
        stakeholder=stakeholder,
        email="person@example.com",
        manual_overrides={"phone_number"},
    )

    assert stakeholder.phone_number == "+1 555-MANUAL"


@pytest.mark.asyncio
async def test_enrich_from_contact_skips_phone_when_contact_has_none() -> None:
    """If CRMContact.phone is None, stakeholder.phone_number stays unchanged."""
    service = StakeholderSyncService(uuid4())
    contact = SimpleNamespace(
        contact_id=uuid4(),
        full_name=None,
        job_title=None,
        phone=None,
    )

    scalars_result = MagicMock()
    scalars_result.first.return_value = contact
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

    stakeholder = SimpleNamespace(
        contact_id=contact.contact_id,
        name=None,
        job_title=None,
        phone_number=None,
    )

    await service._enrich_from_contact(  # noqa: SLF001
        session=session,
        stakeholder=stakeholder,
        email="person@example.com",
    )

    assert stakeholder.phone_number is None


@pytest.mark.asyncio
async def test_enrich_from_contact_does_not_update_phone_snapshot() -> None:
    """CRMContact.phone remains contact-owned even when participant has old data."""
    service = StakeholderSyncService(uuid4())
    contact = SimpleNamespace(
        contact_id=uuid4(),
        full_name=None,
        job_title=None,
        phone="+1 800-UPDATED",
    )

    scalars_result = MagicMock()
    scalars_result.first.return_value = contact
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

    stakeholder = SimpleNamespace(
        contact_id=contact.contact_id,
        name=None,
        job_title=None,
        phone_number="+1 800-OLD",
    )

    changed = await service._enrich_from_contact(  # noqa: SLF001
        session=session,
        stakeholder=stakeholder,
        email="person@example.com",
    )

    assert changed is False
    assert stakeholder.phone_number == "+1 800-OLD"
