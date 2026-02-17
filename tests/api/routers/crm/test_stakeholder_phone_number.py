"""Tests for phone_number field across the stakeholder feature.

Covers:
- API handler: list, detail, create, update endpoints return/accept phone_number
- Service layer: create and update handle phone_number correctly
- Sync service: enriches phone_number from CRMContact, respects manual overrides
- Agent tools: upsert creates/updates phone_number, get_details and list return it
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from src.api.schemas.crm_models import UpdateStakeholderRequest
from src.service.crm.insights.stakeholders_service import (
    StakeholdersService,
)
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


def _service() -> StakeholdersService:
    session = AsyncMock()
    return StakeholdersService(session, uuid4())


@pytest.mark.asyncio
async def test_update_stakeholder_applies_phone_number_and_tracks_override() -> None:
    service = _service()
    stakeholder = SimpleNamespace(
        stakeholder_id=uuid4(),
        deal_id=uuid4(),
        email="person@example.com",
        name="Test",
        job_title=None,
        phone_number=None,
        department=None,
        external_links=None,
        stance="UNKNOWN",
        decision_power="UNKNOWN",
        champion_potential=10.0,
        motivation=None,
        pain_points=None,
        keywords=None,
        last_contact_at=None,
        engagement_score=0.0,
        temperature="UNKNOWN",
        source="MANUAL",
        approval_status="APPROVED",
        stakeholder_data=None,
    )
    service.get_stakeholder = AsyncMock(return_value=stakeholder)  # type: ignore[method-assign]

    request = UpdateStakeholderRequest(phone_number="+1 555-9999")
    result = await service.update_stakeholder(uuid4(), stakeholder.stakeholder_id, request)

    assert result.phone_number == "+1 555-9999"
    assert "phone_number" in stakeholder.stakeholder_data["manual_overrides"]
    service.session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_stakeholder_detail_includes_phone_number() -> None:
    service = _service()
    stakeholder = SimpleNamespace(
        stakeholder_id=uuid4(),
        deal_id=uuid4(),
        contact_id=None,
        email="detail@example.com",
        name="Detail",
        job_title="CTO",
        phone_number="+49 30 12345678",
        department="Engineering",
        external_links=None,
        stance="CHAMPION",
        decision_power="DECISION_MAKER",
        champion_potential=90.0,
        motivation="Scale infra",
        pain_points=["reliability"],
        keywords=["cloud"],
        last_contact_at=None,
        engagement_score=80.0,
        temperature="HOT",
        source="CRM",
        approval_status="APPROVED",
        stakeholder_engagement_history=[],
        created_at=None,
        updated_at=None,
    )
    service.get_stakeholder = AsyncMock(return_value=stakeholder)  # type: ignore[method-assign]

    result = await service.get_stakeholder_detail(uuid4(), stakeholder.stakeholder_id)

    assert result is not None
    assert result["phone_number"] == "+49 30 12345678"


@pytest.mark.asyncio
async def test_enrich_from_contact_sets_phone_number() -> None:
    """Sync service should copy CRMContact.phone to stakeholder.phone_number."""
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
    assert stakeholder.phone_number == "+1 800-555-0123"


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
async def test_enrich_from_contact_updates_phone_when_different() -> None:
    """If CRMContact.phone differs from current, it should update."""
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

    assert changed is True
    assert stakeholder.phone_number == "+1 800-UPDATED"


from contextlib import asynccontextmanager  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from src.agents.tools.crm.upsert_stakeholder import (  # noqa: E402
    UpsertStakeholderInput,
)
from src.agents.tools.crm.upsert_stakeholder import (
    upsert_stakeholder as agent_upsert_stakeholder,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (  # noqa: E402
    DealStakeholder,
)


class _FakeScalarResult:
    def __init__(self, obj: DealStakeholder | None):
        self._obj = obj

    def scalar_one_or_none(self) -> DealStakeholder | None:
        return self._obj


class _FakeSession:
    def __init__(self, records: dict[str, DealStakeholder]):
        self.records = records
        self.pending: DealStakeholder | None = None

    async def execute(self, stmt):
        params = stmt.compile().params
        email_param = next((v for k, v in params.items() if k.startswith("email_")), None)
        email = str(email_param).lower() if email_param else ""
        return _FakeScalarResult(self.records.get(email))

    def add(self, obj: DealStakeholder) -> None:
        self.pending = obj

    async def commit(self) -> None:
        if self.pending is not None:
            self.records[self.pending.email.lower()] = self.pending
            self.pending = None

    async def refresh(self, _obj: DealStakeholder) -> None:
        return None

    async def rollback(self) -> None:
        return None


@dataclass
class _FakeContext:
    organisation_id: object
    deal_id: object
    session: _FakeSession
    metadata: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    @asynccontextmanager
    async def acquire_session(self):
        yield self.session


def _make_context(
    records: dict[str, DealStakeholder] | None = None,
) -> tuple[_FakeContext, dict[str, DealStakeholder]]:
    store: dict[str, DealStakeholder] = records if records is not None else {}
    session = _FakeSession(store)
    metadata = {
        "_internal_guard_loaded": True,
        "_internal_domains": set(),
        "_internal_emails": set(),
    }
    context = _FakeContext(
        organisation_id=uuid4(), deal_id=uuid4(), session=session, metadata=metadata
    )
    return context, store


@pytest.mark.asyncio
async def test_agent_upsert_creates_stakeholder_with_phone_number() -> None:
    context, store = _make_context()

    result = await agent_upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="new@customer.com",
            name="New Person",
            phone_number="+1 555-0001",
        ),
    )

    assert result is not None
    assert result["action"] == "created"
    assert result["phone_number"] == "+1 555-0001"
    assert store["new@customer.com"].phone_number == "+1 555-0001"


@pytest.mark.asyncio
async def test_agent_upsert_fills_missing_phone_number() -> None:
    context, store = _make_context()

    await agent_upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(email="nophone@customer.com", name="No Phone"),
    )
    assert store["nophone@customer.com"].phone_number is None

    result = await agent_upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="nophone@customer.com",
            phone_number="+1 555-0002",
        ),
    )

    assert result is not None
    assert result["action"] == "updated"
    assert "phone_number" in result["updated_fields"]
    assert store["nophone@customer.com"].phone_number == "+1 555-0002"


@pytest.mark.asyncio
async def test_agent_upsert_does_not_overwrite_phone_in_fill_missing_mode() -> None:
    context, store = _make_context()

    await agent_upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="hasphone@customer.com",
            phone_number="+1 555-ORIGINAL",
        ),
    )

    result = await agent_upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="hasphone@customer.com",
            phone_number="+1 555-NEW",
            only_fill_missing=True,
        ),
    )

    assert result is not None
    assert "phone_number" not in result.get("updated_fields", [])
    assert store["hasphone@customer.com"].phone_number == "+1 555-ORIGINAL"


@pytest.mark.asyncio
async def test_agent_upsert_overwrites_phone_when_fill_missing_disabled() -> None:
    context, store = _make_context()

    await agent_upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="overwrite@customer.com",
            phone_number="+1 555-OLD",
        ),
    )

    result = await agent_upsert_stakeholder(
        context=context,  # type: ignore[arg-type]
        input_data=UpsertStakeholderInput(
            email="overwrite@customer.com",
            phone_number="+1 555-NEW",
            only_fill_missing=False,
        ),
    )

    assert result is not None
    assert "phone_number" in result["updated_fields"]
    assert store["overwrite@customer.com"].phone_number == "+1 555-NEW"
