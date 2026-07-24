from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.internal.deal_write_service import DealWriteService


class _FakeResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[object]:
        return self._rows


class _FakeOrgSession:
    def __init__(self, rows: list[object]):
        self._rows = rows
        self.added: list[object] = []

    async def execute(self, _stmt):
        return _FakeResult(self._rows)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


class _QueuedOrgSession(_FakeOrgSession):
    def __init__(self, results: list[list[object]]):
        super().__init__([])
        self._results = results

    async def execute(self, _stmt):
        rows = self._results.pop(0) if self._results else []
        return _FakeResult(rows)


def _contact(
    contact_id,
    *,
    full_name: str | None = "Jane Buyer",
    email: str | None = "jane@example.com",
    additional_emails: list[str] | None = None,
    phone: str | None = None,
    mobile_phone: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        contact_id=contact_id,
        full_name=full_name,
        first_name=None,
        last_name=None,
        email=email,
        additional_emails=additional_emails or [],
        phone=phone,
        mobile_phone=mobile_phone,
    )


def _deal(deal_id, integration_id) -> SimpleNamespace:
    return SimpleNamespace(
        deal_id=deal_id,
        company_name="Acme",
        integration_id=integration_id,
        primary_contact_id=uuid4(),
        primary_contact_name="Old Contact",
        primary_contact_email="old@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )


def _participant(contact_id, *, primary: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        participant_id=uuid4(),
        related_local_id=contact_id,
        related_name=None,
        related_email=None,
        relationship_role="decision_maker",
        is_primary_contact=primary,
        is_deleted=False,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )


@pytest.mark.asyncio
async def test_upsert_company_overwrites_existing_industry_and_country_when_provided() -> None:
    existing_company = SimpleNamespace(
        name="Acme",
        domain="acme.com",
        website=None,
        linkedin_url=None,
        industry="Powerline",
        country="India",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = _FakeOrgSession([existing_company])
    service = AccountWriteService(session, uuid4())

    now = datetime.now(UTC)
    company, fallback = await service.upsert_company_from_account(
        account_payload={
            "name": "Acme",
            "domain": "acme.com",
            "industry": "Telecom",
            "country": "US",
        },
        integration_id=uuid4(),
        now=now,
    )

    assert fallback is None
    assert company is existing_company
    assert existing_company.industry == "Telecom"
    assert existing_company.country == "US"
    assert session.added == [existing_company]


@pytest.mark.asyncio
async def test_upsert_company_preserves_industry_and_country_when_not_provided() -> None:
    existing_company = SimpleNamespace(
        name="Acme",
        domain="acme.com",
        website=None,
        linkedin_url=None,
        industry="Powerline",
        country="India",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = _FakeOrgSession([existing_company])
    service = AccountWriteService(session, uuid4())

    company, fallback = await service.upsert_company_from_account(
        account_payload={"name": "Acme", "domain": "acme.com"},
        integration_id=uuid4(),
        now=datetime.now(UTC),
    )

    assert fallback is None
    assert company is existing_company
    assert existing_company.industry == "Powerline"
    assert existing_company.country == "India"


@pytest.mark.asyncio
async def test_upsert_company_clears_company_fields_when_explicitly_provided_as_empty() -> None:
    existing_company = SimpleNamespace(
        name="Acme",
        domain="acme.com",
        website="https://acme.com",
        linkedin_url="https://linkedin.com/company/acme",
        industry="Powerline",
        country="India",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = _FakeOrgSession([existing_company])
    service = AccountWriteService(session, uuid4())

    company, fallback = await service.upsert_company_from_account(
        account_payload={
            "name": "Acme",
            "domain": "acme.com",
            "website": None,
            "linkedin_url": None,
            "industry": None,
            "country": None,
        },
        integration_id=uuid4(),
        now=datetime.now(UTC),
    )

    assert fallback is None
    assert company is existing_company
    assert existing_company.website is None
    assert existing_company.linkedin_url is None
    assert existing_company.industry is None
    assert existing_company.country is None


@pytest.mark.asyncio
async def test_update_deal_rewrites_nullable_fields_when_none_is_provided() -> None:
    existing_deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Existing Deal",
        amount=5000,
        currency="USD",
        deal_status=CRMDealStatus.OPEN,
        close_date=datetime.now(UTC),
        expected_close_date=datetime.now(UTC),
        closed_won_at=datetime.now(UTC),
        closed_lost_at=None,
        closed_reason="Old reason",
        crm_url="https://crm.example/deals/1",
        source_created_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
        description="Old description",
        deal_type="Old Type",
        priority="High",
        lead_source="Inbound",
        probability=70.0,
        forecast_category="Commit",
        properties={"is_internal_crm": True},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = _FakeOrgSession([])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock(return_value=existing_deal)

    updated = await service.update_deal(
        existing_deal.deal_id,
        {
            "priority": None,
            "description": None,
            "expected_close_date": None,
            "probability": None,
            "forecast_category": None,
        },
    )

    assert updated is existing_deal
    assert existing_deal.priority is None
    assert existing_deal.description is None
    assert existing_deal.close_date is not None
    assert existing_deal.expected_close_date is None
    assert existing_deal.probability is None
    assert existing_deal.forecast_category is None
    assert existing_deal.closed_reason == "Old reason"
    assert existing_deal.deal_status == CRMDealStatus.OPEN


@pytest.mark.asyncio
async def test_update_deal_rejects_actual_close_date_field() -> None:
    service = DealWriteService(
        org_session=_FakeOrgSession([]),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock(
        return_value=SimpleNamespace(
            deal_id=uuid4(),
            name="Existing Deal",
            amount=None,
            currency=None,
            source_created_at=None,
            close_date=None,
            expected_close_date=None,
            description=None,
            deal_type=None,
            priority=None,
            lead_source=None,
            probability=None,
            forecast_category=None,
            properties={"is_internal_crm": True},
            source_modified_at=None,
            synced_at=None,
            last_seen_at=None,
        )
    )

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_deal(uuid4(), {"close_date": None})

    assert "close_date" in exc_info.value.message


@pytest.mark.asyncio
async def test_update_account_rejects_invalid_company_id_before_loading_deal() -> None:
    service = DealWriteService(
        org_session=_FakeOrgSession([]),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_account(uuid4(), {"company_id": "not-a-uuid"})

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Invalid company_id"
    service._load_internal_deal.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_account_rejects_invalid_raw_string_fields_before_loading_deal() -> None:
    service = DealWriteService(
        org_session=_FakeOrgSession([]),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_account(uuid4(), {"website": 123})

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "website must be a string"
    service._load_internal_deal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["name", "domain"])
async def test_update_account_rejects_identity_field_clears_before_loading_deal(
    field_name: str,
) -> None:
    service = DealWriteService(
        org_session=_FakeOrgSession([]),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_account(uuid4(), {field_name: None, "website": "https://acme.com"})

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == f"{field_name} cannot be cleared"
    service._load_internal_deal.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_account_skips_participant_replace_when_company_link_already_exists() -> None:
    session = _FakeOrgSession([])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    deal_id = uuid4()
    company_id = uuid4()
    existing_deal = SimpleNamespace(
        deal_id=deal_id,
        company_id=company_id,
        company_name="Acme",
        company_domain="acme.com",
        integration_id=uuid4(),
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    resolved_company = SimpleNamespace(
        company_id=company_id,
        name="Acme",
        domain="acme.com",
        industry="Powerline",
        website="https://acme.com",
        linkedin_url="https://linkedin.com/company/acme",
        country="India",
    )

    service._load_internal_deal = AsyncMock(return_value=existing_deal)
    service._account_writer.upsert_company_from_account = AsyncMock(
        return_value=(resolved_company, None)
    )
    service._has_active_company_participant = AsyncMock(return_value=True)
    service._replace_company_participant = AsyncMock()

    result = await service.update_account(
        deal_id,
        {
            "name": "Acme",
            "domain": "acme.com",
            "website": None,
            "linkedin_url": None,
            "industry": "Powerline",
            "country": "India",
        },
    )

    assert result is existing_deal
    assert existing_deal.company_industry == "Powerline"
    assert existing_deal.company_website == "https://acme.com"
    assert existing_deal.company_linkedin_url == "https://linkedin.com/company/acme"
    assert existing_deal.company_country == "India"
    service._replace_company_participant.assert_not_called()
    service._has_active_company_participant.assert_awaited_once_with(
        deal=existing_deal,
        company_id=company_id,
    )


def test_company_profile_response_snapshot_supports_sqlmodel_deal() -> None:
    deal = CRMDeal(
        deal_id=uuid4(),
        organisation_id=uuid4(),
        integration_id=uuid4(),
        name="Acme Deal",
    )
    company = SimpleNamespace(
        industry="Powerline",
        website="https://acme.com",
        linkedin_url="https://linkedin.com/company/acme",
        country="India",
    )

    DealWriteService._sync_deal_company_profile_for_response(deal=deal, company=company)

    assert deal.company_industry == "Powerline"
    assert deal.company_website == "https://acme.com"
    assert deal.company_linkedin_url == "https://linkedin.com/company/acme"
    assert deal.company_country == "India"


@pytest.mark.asyncio
async def test_update_account_allows_clearing_company_profile_fields() -> None:
    session = _FakeOrgSession([])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    deal_id = uuid4()
    company_id = uuid4()
    existing_deal = SimpleNamespace(
        deal_id=deal_id,
        company_id=company_id,
        company_name="Acme",
        company_domain="acme.com",
        integration_id=uuid4(),
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    resolved_company = SimpleNamespace(
        company_id=company_id,
        name="Acme",
        domain="acme.com",
        industry=None,
        website=None,
        linkedin_url=None,
        country=None,
    )

    service._load_internal_deal = AsyncMock(return_value=existing_deal)
    service._get_current_company = AsyncMock(return_value=None)
    service._account_writer.upsert_company_from_account = AsyncMock(
        return_value=(resolved_company, None)
    )
    service._has_active_company_participant = AsyncMock(return_value=True)
    service._replace_company_participant = AsyncMock()

    result = await service.update_account(
        deal_id,
        {
            "website": None,
            "linkedin_url": None,
            "industry": None,
            "country": None,
        },
    )

    assert result is existing_deal
    assert existing_deal.company_industry is None
    assert existing_deal.company_website is None
    assert existing_deal.company_linkedin_url is None
    assert existing_deal.company_country is None
    service._account_writer.upsert_company_from_account.assert_awaited_once_with(
        {
            "website": None,
            "linkedin_url": None,
            "industry": None,
            "country": None,
            "name": "Acme",
            "domain": "acme.com",
        },
        existing_deal.integration_id,
        service._account_writer.upsert_company_from_account.await_args.args[2],
    )
    service._replace_company_participant.assert_not_called()


@pytest.mark.asyncio
async def test_update_primary_contact_rejects_invalid_raw_strings_before_loading_deal() -> None:
    service = DealWriteService(
        org_session=_FakeOrgSession([]),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_primary_contact(
            uuid4(),
            primary_contact_name=123,  # type: ignore[arg-type]
            primary_contact_email=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "primary_contact_name must be a string"
    service._load_internal_deal.assert_not_awaited()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_primary_contact(
            uuid4(),
            primary_contact_name=None,
            primary_contact_email=123,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "primary_contact_email must be a string"
    service._load_internal_deal.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_primary_contact_rejects_invalid_email_before_loading_deal() -> None:
    service = DealWriteService(
        org_session=_FakeOrgSession([]),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_primary_contact(
            uuid4(),
            primary_contact_name="Jane Doe",
            primary_contact_email="not-an-email",
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "primary_contact_email must be a valid email"
    service._load_internal_deal.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_primary_contact_clears_internal_link_when_only_fallback_values_exist() -> (
    None
):
    primary_participant = SimpleNamespace(
        is_deleted=False,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = _FakeOrgSession([primary_participant])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    deal_id = uuid4()
    existing_deal = SimpleNamespace(
        deal_id=deal_id,
        company_name="Acme",
        integration_id=uuid4(),
        primary_contact_id=uuid4(),
        primary_contact_name="Old Contact",
        primary_contact_email="old@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )

    service._load_internal_deal = AsyncMock(return_value=existing_deal)
    service._upsert_contact_from_primary = AsyncMock(
        return_value=(None, "New Contact", "new@example.com")
    )

    result = await service.update_primary_contact(
        deal_id,
        primary_contact_name="New Contact",
        primary_contact_email="new@example.com",
    )

    assert result is existing_deal
    assert existing_deal.primary_contact_id is None
    assert existing_deal.primary_contact_name == "New Contact"
    assert existing_deal.primary_contact_email == "new@example.com"
    assert primary_participant.is_deleted is True
    assert primary_participant.source_modified_at is not None
    assert primary_participant.synced_at is not None
    assert primary_participant.last_seen_at is not None
    assert primary_participant in session.added


@pytest.mark.asyncio
async def test_update_primary_contact_can_clear_snapshot_and_participant_links() -> None:
    primary_participant = SimpleNamespace(
        is_deleted=False,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    session = _FakeOrgSession([primary_participant])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    deal_id = uuid4()
    existing_deal = SimpleNamespace(
        deal_id=deal_id,
        company_name="Acme",
        integration_id=uuid4(),
        primary_contact_id=uuid4(),
        primary_contact_name="Old Contact",
        primary_contact_email="old@example.com",
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )

    service._load_internal_deal = AsyncMock(return_value=existing_deal)
    service._record_mutation_event = AsyncMock()

    result = await service.update_primary_contact(
        deal_id,
        primary_contact_name=None,
        primary_contact_email=None,
        clear_primary_contact=True,
    )

    assert result is existing_deal
    assert existing_deal.primary_contact_id is None
    assert existing_deal.primary_contact_name is None
    assert existing_deal.primary_contact_email is None
    assert primary_participant.is_deleted is True
    assert primary_participant.source_modified_at is not None
    assert primary_participant.synced_at is not None
    assert primary_participant.last_seen_at is not None
    assert primary_participant in session.added


@pytest.mark.asyncio
async def test_update_primary_contact_sets_approved_stakeholder_by_contact_id() -> None:
    contact_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    selected_contact = _contact(contact_id, email="Jane@Example.com")
    selected_participant = _participant(contact_id)
    selected_participant.relationship_role = "Economic Buyer"
    old_primary = _participant(uuid4(), primary=True)
    old_primary.relationship_role = "primary_contact"
    session = _QueuedOrgSession([[selected_contact], [selected_participant], [old_primary]])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    existing_deal = _deal(deal_id, integration_id)
    service._load_internal_deal = AsyncMock(return_value=existing_deal)
    service._record_mutation_event = AsyncMock()

    result = await service.update_primary_contact(
        deal_id,
        primary_contact_id=contact_id,
    )

    assert result is existing_deal
    assert existing_deal.primary_contact_id == contact_id
    assert existing_deal.primary_contact_name == "Jane Buyer"
    assert existing_deal.primary_contact_email == "jane@example.com"
    assert selected_participant.is_primary_contact is True
    assert selected_participant.relationship_role == "Economic Buyer"
    assert selected_participant.related_email == "jane@example.com"
    assert old_primary.is_deleted is False
    assert old_primary.is_primary_contact is False
    assert old_primary.relationship_role is None
    assert selected_participant in session.added
    assert old_primary in session.added
    assert existing_deal in session.added


@pytest.mark.asyncio
async def test_update_primary_contact_uses_additional_email_for_contact_id() -> None:
    contact_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    selected_contact = _contact(
        contact_id,
        email=None,
        additional_emails=["  Alternate@Example.com  "],
    )
    selected_participant = _participant(contact_id)
    session = _QueuedOrgSession([[selected_contact], [selected_participant], []])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    existing_deal = _deal(deal_id, integration_id)
    service._load_internal_deal = AsyncMock(return_value=existing_deal)
    service._record_mutation_event = AsyncMock()

    await service.update_primary_contact(deal_id, primary_contact_id=contact_id)

    assert existing_deal.primary_contact_email == "alternate@example.com"
    assert selected_participant.related_email == "alternate@example.com"


@pytest.mark.asyncio
async def test_update_primary_contact_allows_phone_only_contact_id() -> None:
    contact_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    selected_contact = _contact(contact_id, email=None, phone="  +15551234567  ")
    selected_participant = _participant(contact_id)
    session = _QueuedOrgSession([[selected_contact], [selected_participant], []])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    existing_deal = _deal(deal_id, integration_id)
    service._load_internal_deal = AsyncMock(return_value=existing_deal)
    service._record_mutation_event = AsyncMock()

    await service.update_primary_contact(deal_id, primary_contact_id=contact_id)

    assert existing_deal.primary_contact_email is None
    assert existing_deal.primary_contact_phone_number == "+15551234567"
    assert selected_participant.is_primary_contact is True


@pytest.mark.asyncio
async def test_update_primary_contact_rejects_contact_id_without_email_or_phone() -> None:
    contact_id = uuid4()
    deal_id = uuid4()
    integration_id = uuid4()
    selected_contact = _contact(contact_id, email=None, full_name="Jane Buyer")
    session = _QueuedOrgSession([[selected_contact]])
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock(return_value=_deal(deal_id, integration_id))

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_primary_contact(deal_id, primary_contact_id=contact_id)

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Primary contact must have an email or phone number"


@pytest.mark.asyncio
async def test_update_primary_contact_rejects_archived_or_missing_contact_id() -> None:
    service = DealWriteService(
        org_session=_QueuedOrgSession([[]]),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock(return_value=_deal(uuid4(), uuid4()))

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_primary_contact(uuid4(), primary_contact_id=uuid4())

    assert exc_info.value.status_code == 404
    assert exc_info.value.message == "Contact not found"


@pytest.mark.asyncio
async def test_update_primary_contact_rejects_contact_id_not_active_approved_stakeholder() -> None:
    contact_id = uuid4()
    service = DealWriteService(
        org_session=_QueuedOrgSession([[_contact(contact_id)], []]),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock(return_value=_deal(uuid4(), uuid4()))

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.update_primary_contact(uuid4(), primary_contact_id=contact_id)

    assert exc_info.value.status_code == 400
    assert (
        exc_info.value.message
        == "Primary contact must be an active approved Buying Committee stakeholder on this deal"
    )
