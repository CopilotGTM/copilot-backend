"""Account write mutation-event tests."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.crm.accounts.contracts import (
    AccountCreateCommand,
    AccountWriteContext,
    AccountWriteServiceError,
)
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.mutations.policies import AccountMutationPolicy


class _FakeScalars:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[object]:
        return self._rows


class _FakeResult:
    def __init__(self, *, first: object | None = None, all_rows: list[object] | None = None):
        if all_rows is None:
            all_rows = [] if first is None else [first]
        self._scalars = _FakeScalars(all_rows)

    def scalars(self) -> _FakeScalars:
        return self._scalars


class _AccountSession:
    def __init__(self, *results: _FakeResult) -> None:
        self._results = list(results)
        self.added: list[object] = []
        self.flush_count = 0
        self.refreshed: list[object] = []

    async def execute(self, _statement: object) -> _FakeResult:
        if not self._results:
            return _FakeResult()
        return self._results.pop(0)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1

    async def refresh(self, obj: object) -> None:
        self.refreshed.append(obj)


def _integration(org_id):
    return KnowledgeIntegration(
        integration_id=uuid4(),
        organisation_id=org_id,
        created_by_user_id=uuid4(),
        created_by_admin=False,
        integration_type=IntegrationType.INTERNAL_CRM,
        integration_scope=IntegrationScope.ORGANIZATION,
        name="Copilot CRM",
        credentials={},
        connection_metadata={},
        is_active=True,
    )


def _company(org_id, *, integration_id=None, **overrides) -> CRMCompany:
    values = {
        "company_id": uuid4(),
        "organisation_id": org_id,
        "integration_id": integration_id or uuid4(),
        "name": "Acme",
        "domain": "acme.com",
        "website": "https://acme.com",
        "is_deleted": False,
    }
    values.update(overrides)
    return CRMCompany(**values)


def _events(session: _AccountSession) -> list[CRMDealMutationEvent]:
    return [obj for obj in session.added if isinstance(obj, CRMDealMutationEvent)]


def test_account_create_policy_records_audit_safe_initial_fields() -> None:
    org_id = uuid4()
    account = _company(
        org_id,
        name="Acme",
        domain="acme.com",
        website="https://acme.com",
        industry="Software",
        properties={"source": "provider"},
        metrics={"arr": 1000},
    )

    changed_fields = AccountMutationPolicy.build_create_changed_fields(account)

    assert changed_fields["name"] == {"old": None, "new": "Acme"}
    assert changed_fields["domain"] == {"old": None, "new": "acme.com"}
    assert changed_fields["industry"] == {"old": None, "new": "Software"}
    assert "properties" not in changed_fields
    assert "metrics" not in changed_fields
    assert AccountMutationPolicy.semantic_for_create() == ["crm.account.created"]


def test_account_update_policy_keeps_profile_fields_audit_only() -> None:
    changed_fields = {
        "industry": AccountMutationPolicy.changed_field("industry", "Services", "Software"),
        "linkedin_url": AccountMutationPolicy.changed_field(
            "linkedin_url", None, "https://linkedin.com/company/acme"
        ),
    }
    changed_fields = {key: value for key, value in changed_fields.items() if value}

    assert changed_fields == {
        "industry": {"old": "Services", "new": "Software"},
        "linkedin_url": {"old": None, "new": "https://linkedin.com/company/acme"},
    }
    assert AccountMutationPolicy.semantic_for_update(changed_fields) == []


def test_account_update_policy_exposes_only_curated_semantic_fields() -> None:
    owner_id = uuid4()
    changed_fields = {
        "domain": AccountMutationPolicy.changed_field("domain", "old.com", "new.com"),
        "owner_id": AccountMutationPolicy.changed_field("owner_id", None, owner_id),
        "owner_name": AccountMutationPolicy.changed_field("owner_name", None, "Ada Owner"),
        "lifecycle_stage": AccountMutationPolicy.changed_field(
            "lifecycle_stage", "lead", "customer"
        ),
    }
    changed_fields = {key: value for key, value in changed_fields.items() if value}

    assert AccountMutationPolicy.semantic_for_update(changed_fields) == [
        "crm.account.domain_changed",
        "crm.account.owner_changed",
        "crm.account.lifecycle_stage_changed",
    ]
    assert AccountMutationPolicy.changed_field("industry", "Software", "Software") is None
    assert AccountMutationPolicy.changed_field("properties", {}, {"raw": True}) is None


@pytest.mark.asyncio
async def test_create_account_records_account_created_mutation_event() -> None:
    org_id = uuid4()
    integration = _integration(org_id)
    actor_id = uuid4()
    session = _AccountSession(_FakeResult(all_rows=[]), _FakeResult(first=integration))
    service = AccountWriteService(session, org_id)
    service._mutation_events._automation_events = AsyncMock()

    result = await service.create_account(
        AccountCreateCommand(
            name="Acme",
            domain="https://www.acme.com/about",
            industry="Software",
        ),
        context=AccountWriteContext(source="api", actor_user_id=actor_id),
    )

    events = _events(session)
    assert len(events) == 1
    assert events[0].entity_type == "account"
    assert events[0].entity_id == result.account.company_id
    assert events[0].event_type == "crm.account.created"
    assert events[0].operation == "create_account"
    assert events[0].source == "api"
    assert events[0].actor_user_id == actor_id
    assert events[0].semantic_event_types == ["crm.account.created"]
    assert events[0].changed_fields["name"] == {"old": None, "new": "Acme"}
    assert events[0].changed_fields["domain"] == {"old": None, "new": "acme.com"}
    assert events[0].changed_fields["industry"] == {"old": None, "new": "Software"}


@pytest.mark.asyncio
async def test_create_account_records_normalized_domain_not_raw_input() -> None:
    org_id = uuid4()
    integration = _integration(org_id)
    session = _AccountSession(_FakeResult(all_rows=[]), _FakeResult(first=integration))
    service = AccountWriteService(session, org_id)
    service._mutation_events._automation_events = AsyncMock()

    await service.create_account(
        AccountCreateCommand(name=None, domain="HTTPS://WWW.Acme.COM/path"),
        context=AccountWriteContext(source="agent_tool"),
    )

    event = _events(session)[0]
    assert event.changed_fields["name"] == {"old": None, "new": "acme.com"}
    assert event.changed_fields["domain"] == {"old": None, "new": "acme.com"}


@pytest.mark.asyncio
async def test_create_account_duplicate_domain_records_no_mutation_event() -> None:
    org_id = uuid4()
    existing = _company(org_id, domain="acme.com")
    session = _AccountSession(_FakeResult(all_rows=[existing]))
    service = AccountWriteService(session, org_id)

    with pytest.raises(AccountWriteServiceError):
        await service.create_account(
            AccountCreateCommand(name="Acme", domain="https://www.acme.com"),
            context=AccountWriteContext(source="api"),
        )

    assert _events(session) == []


@pytest.mark.asyncio
async def test_create_account_duplicate_name_records_no_mutation_event() -> None:
    org_id = uuid4()
    existing = _company(org_id, name="Acme", domain=None)
    session = _AccountSession(_FakeResult(all_rows=[existing]))
    service = AccountWriteService(session, org_id)

    with pytest.raises(AccountWriteServiceError):
        await service.create_account(
            AccountCreateCommand(name="Acme"),
            context=AccountWriteContext(source="api"),
        )

    assert _events(session) == []


@pytest.mark.asyncio
async def test_update_account_profile_edit_records_audit_only_mutation_event() -> None:
    org_id = uuid4()
    account = _company(org_id, industry="Services")
    session = _AccountSession(_FakeResult(first=account))
    service = AccountWriteService(session, org_id)

    await service.update_account(
        account.company_id,
        {"industry": "Software"},
        context=AccountWriteContext(source="api"),
    )

    events = _events(session)
    assert len(events) == 1
    assert events[0].event_type == "crm.account.updated"
    assert events[0].operation == "update_account"
    assert events[0].semantic_event_types == []
    assert events[0].changed_fields == {"industry": {"old": "Services", "new": "Software"}}


@pytest.mark.asyncio
async def test_update_account_domain_change_emits_domain_changed_event() -> None:
    org_id = uuid4()
    account = _company(org_id, domain="old-acme.com")
    session = _AccountSession(_FakeResult(first=account), _FakeResult(first=None))
    service = AccountWriteService(session, org_id)

    await service.update_account(
        account.company_id,
        {"domain": "https://www.acme.com/path"},
        context=AccountWriteContext(source="api"),
    )

    event = _events(session)[0]
    assert event.semantic_event_types == ["crm.account.domain_changed"]
    assert event.changed_fields == {"domain": {"old": "old-acme.com", "new": "acme.com"}}


@pytest.mark.asyncio
async def test_update_account_website_derived_domain_records_both_changed_fields() -> None:
    org_id = uuid4()
    account = _company(org_id, domain="old-acme.com", website="https://old-acme.com")
    session = _AccountSession(_FakeResult(first=account), _FakeResult(first=None))
    service = AccountWriteService(session, org_id)

    await service.update_account(
        account.company_id,
        {"website": "https://www.acme.com/about"},
        context=AccountWriteContext(source="agent_tool"),
    )

    event = _events(session)[0]
    assert event.source == "agent_tool"
    assert event.semantic_event_types == ["crm.account.domain_changed"]
    assert event.changed_fields == {
        "domain": {"old": "old-acme.com", "new": "acme.com"},
        "website": {"old": "https://old-acme.com", "new": "https://www.acme.com/about"},
    }


@pytest.mark.asyncio
async def test_update_account_owner_id_change_emits_owner_changed_event() -> None:
    org_id = uuid4()
    owner_id = uuid4()
    account = _company(org_id, owner_id=None, owner_name=None, owner_email=None)
    session = _AccountSession(_FakeResult(first=account))
    service = AccountWriteService(session, org_id)

    await service.update_account(
        account.company_id,
        {"owner_id": owner_id, "owner_name": "Ada Owner", "owner_email": "ada@example.com"},
        context=AccountWriteContext(source="api"),
    )

    event = _events(session)[0]
    assert event.semantic_event_types == ["crm.account.owner_changed"]
    assert event.changed_fields["owner_id"] == {"old": None, "new": str(owner_id)}
    assert event.changed_fields["owner_name"] == {"old": None, "new": "Ada Owner"}
    assert event.changed_fields["owner_email"] == {"old": None, "new": "ada@example.com"}


@pytest.mark.asyncio
async def test_update_account_owner_display_only_is_audit_only() -> None:
    org_id = uuid4()
    account = _company(org_id, owner_id=uuid4(), owner_name="Old Owner")
    session = _AccountSession(_FakeResult(first=account))
    service = AccountWriteService(session, org_id)

    await service.update_account(
        account.company_id,
        {"owner_name": "New Owner"},
        context=AccountWriteContext(source="api"),
    )

    event = _events(session)[0]
    assert event.semantic_event_types == []
    assert event.changed_fields == {"owner_name": {"old": "Old Owner", "new": "New Owner"}}


@pytest.mark.asyncio
async def test_update_account_lifecycle_stage_change_emits_lifecycle_stage_event() -> None:
    org_id = uuid4()
    account = _company(org_id, lifecycle_stage="lead")
    session = _AccountSession(_FakeResult(first=account))
    service = AccountWriteService(session, org_id)

    await service.update_account(
        account.company_id,
        {"lifecycle_stage": "customer"},
        context=AccountWriteContext(source="api"),
    )

    event = _events(session)[0]
    assert event.semantic_event_types == ["crm.account.lifecycle_stage_changed"]
    assert event.changed_fields == {"lifecycle_stage": {"old": "lead", "new": "customer"}}


@pytest.mark.asyncio
async def test_update_account_same_value_records_no_mutation_event() -> None:
    org_id = uuid4()
    account = _company(org_id, industry="Software")
    session = _AccountSession(_FakeResult(first=account))
    service = AccountWriteService(session, org_id)

    await service.update_account(
        account.company_id,
        {"industry": "Software"},
        context=AccountWriteContext(source="api"),
    )

    assert _events(session) == []


@pytest.mark.asyncio
async def test_update_account_duplicate_domain_records_no_mutation_event() -> None:
    org_id = uuid4()
    account = _company(org_id, domain="old-acme.com")
    duplicate = _company(org_id, domain="acme.com")
    session = _AccountSession(_FakeResult(first=account), _FakeResult(first=duplicate))
    service = AccountWriteService(session, org_id)

    with pytest.raises(AccountWriteServiceError):
        await service.update_account(
            account.company_id,
            {"domain": "https://www.acme.com"},
            context=AccountWriteContext(source="api"),
        )

    assert _events(session) == []
