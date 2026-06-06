from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.service.crm.internal.deal_write_service import DealWriteService


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> "_ScalarResult":
        return self

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[object]:
        return self._rows


@pytest.mark.asyncio
async def test_selected_external_account_is_copied_to_internal_crm_company() -> None:
    org_id = uuid4()
    actor_user_id = uuid4()
    internal_integration_id = uuid4()
    external_integration_id = uuid4()
    external_company_id = uuid4()
    now = datetime.now(UTC)
    external_company = CRMCompany(
        company_id=external_company_id,
        organisation_id=org_id,
        integration_id=external_integration_id,
        name="Acme Corp",
        domain="acme.com",
        website="https://acme.com",
        industry="Software",
        country="US",
        source_created_at=now,
        source_modified_at=now,
        synced_at=now,
        last_seen_at=now,
    )
    session = AsyncMock()
    session.get = AsyncMock(return_value=external_company)
    session.execute = AsyncMock(return_value=_ScalarResult([]))
    session.add = Mock()
    session.flush = AsyncMock()
    service = DealWriteService(
        org_session=session,
        master_session=AsyncMock(),
        organisation_id=org_id,
        actor_user_id=actor_user_id,
    )

    company, ambiguous_name = await service._upsert_company_from_account(  # noqa: SLF001
        {"company_id": external_company_id},
        internal_integration_id,
        now,
    )

    assert ambiguous_name is None
    assert company is not None
    assert company.company_id != external_company_id
    assert company.integration_id == internal_integration_id
    assert company.name == "Acme Corp"
    assert company.domain == "acme.com"
    assert company.properties["copied_from_company_id"] == str(external_company_id)
    session.add.assert_called_with(company)
    session.flush.assert_awaited()


@pytest.mark.asyncio
async def test_selected_external_contact_participant_is_copied_before_linking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_user_id = uuid4()
    internal_integration_id = uuid4()
    external_integration_id = uuid4()
    internal_company_id = uuid4()
    external_contact_id = uuid4()
    now = datetime.now(UTC)
    internal_company = CRMCompany(
        company_id=internal_company_id,
        organisation_id=org_id,
        integration_id=internal_integration_id,
        name="Acme Corp",
        domain="acme.com",
    )
    external_contact = CRMContact(
        contact_id=external_contact_id,
        organisation_id=org_id,
        integration_id=external_integration_id,
        first_name="Buyer",
        last_name="Person",
        full_name="Buyer Person",
        email="buyer@acme.com",
        job_title="VP Sales",
        company_name="Acme Corp",
        company_id=uuid4(),
        source_created_at=now,
        source_modified_at=now,
        synced_at=now,
        last_seen_at=now,
    )
    deal = CRMDeal(
        deal_id=uuid4(),
        organisation_id=org_id,
        integration_id=internal_integration_id,
        name="Acme Renewal",
        company_id=internal_company_id,
        company_name="Acme Corp",
    )
    session = AsyncMock()
    session.get = AsyncMock(return_value=external_contact)
    session.execute = AsyncMock(side_effect=[_ScalarResult([internal_company]), _ScalarResult([])])
    session.add = Mock()
    session.flush = AsyncMock()
    captured_payloads: list[dict[str, object]] = []

    class _ParticipantService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def add_participants(
            self,
            _deal_id: object,
            payloads: list[dict[str, object]],
        ) -> None:
            captured_payloads.extend(payloads)

    monkeypatch.setattr(
        "src.service.crm.internal.deal_participant_service.DealParticipantService",
        _ParticipantService,
    )
    service = DealWriteService(
        org_session=session,
        master_session=AsyncMock(),
        organisation_id=org_id,
        actor_user_id=actor_user_id,
    )

    await service._add_contact_participants(  # noqa: SLF001
        deal,
        [{"contact_id": external_contact_id}],
    )

    added_contacts = [
        call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], CRMContact)
    ]
    assert len(added_contacts) == 1
    copied_contact = added_contacts[0]
    assert copied_contact.contact_id != external_contact_id
    assert copied_contact.integration_id == internal_integration_id
    assert copied_contact.company_id == internal_company_id
    assert copied_contact.properties["copied_from_contact_id"] == str(external_contact_id)
    assert captured_payloads == [
        {
            "participant_type": "contact",
            "related_id": copied_contact.contact_id,
            "relationship_role": None,
        }
    ]
