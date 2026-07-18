from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import src.api.routers.crm.contacts_handler as contacts_handler
from fastapi import BackgroundTasks
from fastapi import Response as FastAPIResponse
from pydantic import ValidationError
from src.api.routers.crm.contacts_handler import (
    create_contact,
    delete_contact,
    get_contact_detail,
    list_contacts,
    search_contact_create_candidates,
    update_contact,
)
from src.api.schemas.crm_contact_models import CreateContactRequest, UpdateContactRequest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStatus,
    CRMParticipantType,
)
from src.service.crm.contacts.contracts import (
    ContactArchiveImpact,
    ContactArchiveResult,
    ContactCreateCommand,
    ContactWriteResult,
    ContactWriteServiceError,
)


class _FakeContactReadService:
    def __init__(self, rows: list[dict], total_count: int):
        self.rows = rows
        self.total_count = total_count

    async def list_contacts(self, **kwargs):
        self.request = kwargs
        return self.rows, self.total_count


class _FakeContactSearchService:
    def __init__(self, rows: list[dict], next_cursor: str | None):
        self.rows = rows
        self.next_cursor = next_cursor

    async def search_contacts(self, **kwargs):
        self.request = kwargs
        return self.rows, self.next_cursor

    async def search_contacts_for_create(self, **kwargs):
        self.request = kwargs
        return self.rows, self.next_cursor


class _FakeContactDetailService:
    def __init__(self, result: dict):
        self.result = result
        self.organisation_id = result["contact"].organisation_id

    async def get_contact_detail(self, contact_id):
        self.contact_id = contact_id
        return self.result


class _FakeContactWriteService:
    def __init__(self, contact: CRMContact):
        self.contact = contact
        self.organisation_id = getattr(contact, "organisation_id", None)
        self.org_session = SimpleNamespace(commit=_AsyncCall(), rollback=_AsyncCall())
        self.calls: list[dict] = []

    async def create_contact(self, command: ContactCreateCommand, **kwargs):
        self.calls.append({"command": command, **kwargs})
        return ContactWriteResult(contact=self.contact, created=True, matched_by="created")

    async def delete_contact(self, contact_id, **kwargs):
        self.calls.append({"contact_id": contact_id, **kwargs})
        return ContactArchiveResult(
            contact=self.contact,
            impact=ContactArchiveImpact(cleared_deal_primary_contacts=1),
        )

    async def update_contact(self, contact_id, updates, **kwargs):
        """Apply explicit update fields so route tests can inspect background follow-up work."""
        self.calls.append({"contact_id": contact_id, "updates": dict(updates), **kwargs})
        for field, value in updates.items():
            setattr(self.contact, field, value)
        return ContactWriteResult(contact=self.contact, created=False, matched_by="explicit_id")


class _AsyncCall:
    def __init__(self, on_call=None) -> None:
        self.count = 0
        self.on_call = on_call

    async def __call__(self) -> None:
        self.count += 1
        if self.on_call:
            self.on_call()


class _CommitExpiringContact:
    """Test double for async SQLAlchemy objects that expire attributes on commit."""

    _tracked_fields = {
        "contact_id",
        "first_name",
        "last_name",
        "full_name",
        "email",
        "job_title",
        "phone",
        "company_name",
        "company_id",
        "lifecycle_stage",
        "owner_name",
        "created_at",
        "updated_at",
    }

    def __init__(self, **fields):
        self.expired = False
        for key, value in fields.items():
            setattr(self, key, value)

    def __getattribute__(self, name):
        if name in object.__getattribute__(self, "_tracked_fields") and object.__getattribute__(
            self, "expired"
        ):
            raise AssertionError(f"{name} was accessed after commit")
        return object.__getattribute__(self, name)


def test_create_contact_request_accepts_email_without_name() -> None:
    request = CreateContactRequest(
        email="buyer@example.com",
        job_title="CFO",
        linkedin_url="https://linkedin.com/in/buyer",
    )

    assert request.email == "buyer@example.com"
    assert request.name is None
    assert request.job_title == "CFO"
    assert request.linkedin_url == "https://linkedin.com/in/buyer"


def test_create_contact_request_accepts_linkedin_without_email() -> None:
    """Contact create treats LinkedIn as a resolvable identity input."""
    request = CreateContactRequest(linkedin_url="https://linkedin.com/in/buyer")

    assert request.email is None
    assert request.linkedin_url == "https://linkedin.com/in/buyer"


def test_create_contact_request_accepts_name_without_email_or_linkedin() -> None:
    """Name-only contact create is a non-researchable placeholder."""
    request = CreateContactRequest(name="Jane Buyer")

    assert request.name == "Jane Buyer"
    assert request.email is None
    assert request.linkedin_url is None


def test_create_contact_request_rejects_company_linkedin_url() -> None:
    """Contact create should not accept a company page as person identity."""
    with pytest.raises(ValidationError) as exc_info:
        CreateContactRequest(linkedin_url="https://www.linkedin.com/company/acme")

    assert "Use a LinkedIn profile URL for contacts and stakeholders" in str(exc_info.value)


def test_update_contact_request_rejects_company_linkedin_url() -> None:
    """Contact update should keep account LinkedIn URLs off person records."""
    with pytest.raises(ValidationError) as exc_info:
        UpdateContactRequest(linkedin_url="https://www.linkedin.com/company/acme")

    assert "Use a LinkedIn profile URL for contacts and stakeholders" in str(exc_info.value)


def test_create_contact_request_rejects_removed_api_fields() -> None:
    with pytest.raises(ValidationError):
        CreateContactRequest(
            email="buyer@example.com",
            name="Buyer",
            phone="+15555550100",
        )


@pytest.mark.asyncio
async def test_create_contact_handler_passes_create_command_to_service(monkeypatch) -> None:
    org_id = uuid4()
    user_id = uuid4()
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@example.com",
        full_name=None,
        created_at=now,
        updated_at=now,
    )
    service = _FakeContactWriteService(contact)

    async def noop_dispatch(**_kwargs):
        return None

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        noop_dispatch,
        raising=False,
    )

    result = await create_contact(
        body=CreateContactRequest(
            email="buyer@example.com",
            job_title="CFO",
            linkedin_url="https://linkedin.com/in/buyer",
        ),
        response=FastAPIResponse(),
        background_tasks=BackgroundTasks(),
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.data.email == "buyer@example.com"  # type: ignore[union-attr]
    command = service.calls[0]["command"]
    assert command == ContactCreateCommand(
        email="buyer@example.com",
        job_title="CFO",
        linkedin_url="https://linkedin.com/in/buyer",
    )
    assert "phone" not in service.calls[0]
    assert "company_id" not in service.calls[0]
    assert service.org_session.commit.count == 1


@pytest.mark.asyncio
async def test_create_contact_handler_dispatches_research_after_commit(monkeypatch) -> None:
    org_id = uuid4()
    user_id = uuid4()
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@example.com",
        created_at=now,
        updated_at=now,
    )
    service = _FakeContactWriteService(contact)
    calls: list[dict] = []

    async def fake_dispatch(**kwargs):
        calls.append(
            {
                **kwargs,
                "commit_count_at_dispatch": service.org_session.commit.count,
            }
        )

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        fake_dispatch,
        raising=False,
    )

    background_tasks = BackgroundTasks()
    result = await create_contact(
        body=CreateContactRequest(email="buyer@example.com"),
        response=FastAPIResponse(),
        background_tasks=background_tasks,
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.data.email == "buyer@example.com"  # type: ignore[union-attr]
    assert calls == []
    await background_tasks()
    assert calls == [
        {
            "organisation_id": org_id,
            "contact_id": contact.contact_id,
            "user_id": user_id,
            "trigger": "contact_created",
            "commit_count_at_dispatch": 1,
        }
    ]


@pytest.mark.asyncio
async def test_create_contact_handler_dispatches_linkedin_contact_without_email(
    monkeypatch,
) -> None:
    """LinkedIn-resolved contacts should reach centralized research dispatch without email."""
    org_id = uuid4()
    user_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email=None,
        full_name="LinkedIn Buyer",
        linkedin_url="https://linkedin.com/in/buyer",
        profile_enrichment={"provider": "apollo", "name": "LinkedIn Buyer"},
    )
    service = _FakeContactWriteService(contact)
    calls: list[dict] = []

    async def fake_dispatch(**kwargs):
        calls.append(
            {
                **kwargs,
                "commit_count_at_dispatch": service.org_session.commit.count,
            }
        )

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        fake_dispatch,
        raising=False,
    )

    background_tasks = BackgroundTasks()
    result = await create_contact(
        body=CreateContactRequest(linkedin_url="https://linkedin.com/in/buyer"),
        response=FastAPIResponse(),
        background_tasks=background_tasks,
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.data.email is None  # type: ignore[union-attr]
    assert calls == []
    await background_tasks()
    assert calls == [
        {
            "organisation_id": org_id,
            "contact_id": contact.contact_id,
            "user_id": user_id,
            "trigger": "contact_created",
            "commit_count_at_dispatch": 1,
        }
    ]


@pytest.mark.asyncio
async def test_create_contact_response_tolerates_missing_updated_at(monkeypatch) -> None:
    org_id = uuid4()
    user_id = uuid4()
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    contact = SimpleNamespace(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        first_name=None,
        last_name=None,
        full_name="LinkedIn Buyer",
        email="buyer@example.com",
        job_title=None,
        phone=None,
        company_name=None,
        company_id=None,
        lifecycle_stage=None,
        created_at=now,
    )
    service = _FakeContactWriteService(contact)  # type: ignore[arg-type]

    async def noop_dispatch(**_kwargs):
        return None

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        noop_dispatch,
        raising=False,
    )

    result = await create_contact(
        body=CreateContactRequest(linkedin_url="https://linkedin.com/in/buyer"),
        response=FastAPIResponse(),
        background_tasks=BackgroundTasks(),
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    assert result.data.updated_at == now  # type: ignore[union-attr]
    assert service.org_session.commit.count == 1


@pytest.mark.asyncio
async def test_create_contact_handler_uses_precommit_snapshot_for_response(monkeypatch) -> None:
    """Async SQLAlchemy expires rows on commit; response shaping must not lazy-load them."""
    org_id = uuid4()
    user_id = uuid4()
    contact_id = uuid4()
    now = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
    contact = _CommitExpiringContact(
        contact_id=contact_id,
        first_name="Jane",
        last_name="Buyer",
        full_name="Jane Buyer",
        email="jane@example.com",
        job_title="CFO",
        phone=None,
        company_name=None,
        company_id=None,
        lifecycle_stage=None,
        owner_name=None,
        created_at=now,
        updated_at=now,
    )
    service = _FakeContactWriteService(contact)  # type: ignore[arg-type]
    service.org_session.commit = _AsyncCall(on_call=lambda: setattr(contact, "expired", True))
    calls: list[dict] = []

    async def fake_dispatch(**kwargs):
        calls.append({**kwargs, "commit_count_at_dispatch": service.org_session.commit.count})

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        fake_dispatch,
        raising=False,
    )

    background_tasks = BackgroundTasks()
    result = await create_contact(
        body=CreateContactRequest(email="jane@example.com"),
        response=FastAPIResponse(),
        background_tasks=background_tasks,
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.data.contact_id == contact_id  # type: ignore[union-attr]
    assert result.data.email == "jane@example.com"  # type: ignore[union-attr]
    assert calls == []
    await background_tasks()
    assert calls == [
        {
            "organisation_id": org_id,
            "contact_id": contact_id,
            "user_id": user_id,
            "trigger": "contact_created",
            "commit_count_at_dispatch": 1,
        }
    ]


@pytest.mark.asyncio
async def test_create_contact_handler_skips_research_for_name_only_contact(monkeypatch) -> None:
    """Name-only contacts have no email/domain identity, so no research job should be queued."""
    org_id = uuid4()
    user_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email=None,
        full_name="Jane Buyer",
    )
    service = _FakeContactWriteService(contact)

    async def fail_contact_dispatch(**_kwargs):
        raise AssertionError("name-only contacts should not dispatch contact research")

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        fail_contact_dispatch,
        raising=False,
    )

    result = await create_contact(
        body=CreateContactRequest(name="Jane Buyer"),
        response=FastAPIResponse(),
        background_tasks=BackgroundTasks(),
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.data.email is None  # type: ignore[union-attr]
    assert service.org_session.commit.count == 1


@pytest.mark.asyncio
async def test_create_contact_handler_dispatches_account_research_for_created_account(
    monkeypatch,
) -> None:
    org_id = uuid4()
    user_id = uuid4()
    company_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@example.com",
    )
    service = _FakeContactWriteService(contact)

    async def create_contact_with_account(*args, **kwargs):
        result = await _FakeContactWriteService.create_contact(service, *args, **kwargs)
        return ContactWriteResult(
            contact=result.contact,
            created=True,
            matched_by="created",
            created_account_id=company_id,
        )

    service.create_contact = create_contact_with_account
    contact_calls: list[dict] = []
    account_calls: list[dict] = []

    async def fake_contact_dispatch(**kwargs):
        contact_calls.append(kwargs)

    async def fake_account_dispatch(**kwargs):
        account_calls.append(
            {
                **kwargs,
                "commit_count_at_dispatch": service.org_session.commit.count,
            }
        )

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        fake_contact_dispatch,
        raising=False,
    )
    monkeypatch.setattr(
        contacts_handler,
        "dispatch_account_research_async",
        fake_account_dispatch,
        raising=False,
    )

    background_tasks = BackgroundTasks()
    result = await create_contact(
        body=CreateContactRequest(email="buyer@example.com"),
        response=FastAPIResponse(),
        background_tasks=background_tasks,
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    assert contact_calls == []
    assert account_calls == []
    await background_tasks()
    assert len(contact_calls) == 1
    assert account_calls == [
        {
            "organisation_id": org_id,
            "company_id": company_id,
            "user_id": user_id,
            "trigger": "contact_created_account",
            "commit_count_at_dispatch": 1,
        }
    ]


@pytest.mark.asyncio
async def test_create_contact_handler_dispatches_account_research_for_linked_account(
    monkeypatch,
) -> None:
    """Contact create should hand linked accounts to the account dispatcher after commit."""
    org_id = uuid4()
    user_id = uuid4()
    company_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@example.com",
        company_id=company_id,
        company_name="Acme",
    )
    service = _FakeContactWriteService(contact)
    contact_calls: list[dict] = []
    account_calls: list[dict] = []

    async def fake_contact_dispatch(**kwargs):
        contact_calls.append(kwargs)

    async def fake_account_dispatch(**kwargs):
        account_calls.append(
            {
                **kwargs,
                "commit_count_at_dispatch": service.org_session.commit.count,
            }
        )

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        fake_contact_dispatch,
        raising=False,
    )
    monkeypatch.setattr(
        contacts_handler,
        "dispatch_account_research_async",
        fake_account_dispatch,
        raising=False,
    )

    background_tasks = BackgroundTasks()
    result = await create_contact(
        body=CreateContactRequest(email="buyer@example.com"),
        response=FastAPIResponse(),
        background_tasks=background_tasks,
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    assert contact_calls == []
    assert account_calls == []
    await background_tasks()
    assert len(contact_calls) == 1
    assert account_calls == [
        {
            "organisation_id": org_id,
            "company_id": company_id,
            "user_id": user_id,
            "trigger": "contact_created_account",
            "commit_count_at_dispatch": 1,
        }
    ]


@pytest.mark.asyncio
async def test_create_contact_handler_ignores_research_dispatch_failure(monkeypatch) -> None:
    org_id = uuid4()
    user_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="buyer@example.com",
    )
    service = _FakeContactWriteService(contact)

    async def failing_dispatch(**_kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(
        contacts_handler,
        "dispatch_contact_research_async",
        failing_dispatch,
        raising=False,
    )

    background_tasks = BackgroundTasks()
    result = await create_contact(
        body=CreateContactRequest(email="buyer@example.com"),
        response=FastAPIResponse(),
        background_tasks=background_tasks,
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.data.email == "buyer@example.com"  # type: ignore[union-attr]
    assert service.org_session.commit.count == 1
    assert service.org_session.rollback.count == 0
    await background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_email", "expected_linkedin"),
    [
        (UpdateContactRequest(email="buyer@acme.com"), "buyer@acme.com", None),
        (
            UpdateContactRequest(linkedin_url="https://linkedin.com/in/buyer"),
            None,
            "https://linkedin.com/in/buyer",
        ),
    ],
)
async def test_identity_addition_forces_research_after_update(
    monkeypatch,
    body,
    expected_email,
    expected_linkedin,
) -> None:
    """Either newly supplied identity should retry a contact that still lacks research."""
    org_id = uuid4()
    user_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email=None,
        linkedin_url=None,
        contact_research=None,
    )
    service = _FakeContactWriteService(contact)
    calls: list[dict] = []

    class _SourceReader:
        def __init__(self, *_args):
            pass

        async def resolve_source_for_contact(self, _integration_id):
            return "manual"

    async def fake_dispatch(**kwargs):
        """Capture the retry inputs queued for the newly added identity."""
        calls.append(kwargs)

    monkeypatch.setattr(contacts_handler, "ContactReadService", _SourceReader)
    monkeypatch.setattr(contacts_handler, "dispatch_contact_research_async", fake_dispatch)
    background_tasks = BackgroundTasks()
    result = await update_contact(
        contact_id=contact.contact_id,
        body=body,
        response=FastAPIResponse(),
        background_tasks=background_tasks,
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    assert contact.email == expected_email
    assert contact.linkedin_url == expected_linkedin
    assert calls == []
    await background_tasks()
    assert calls == [
        {
            "organisation_id": org_id,
            "contact_id": contact.contact_id,
            "user_id": user_id,
            "trigger": "contact_identity_added",
            "force_refresh": True,
        }
    ]


@pytest.mark.asyncio
async def test_list_contacts_returns_new_list_only_contract() -> None:
    contact_id = uuid4()
    company_id = uuid4()
    owner_id = uuid4()
    now = datetime(2026, 6, 20, 15, 45, tzinfo=UTC)
    enriched_at = datetime(2026, 6, 19, 8, 15, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        contact_id=contact_id,
        first_name="Jane",
        last_name="Doe",
        full_name="Jane Doe",
        email="jane@example.com",
        additional_emails=["jane.alt@example.com"],
        job_title="VP Sales",
        phone="+1 415 555 0100",
        mobile_phone="+1 415 555 0101",
        linkedin_url="https://linkedin.com/in/janedoe",
        lifecycle_stage="lead",
        lead_status="new",
        marketing_status="subscribed",
        profile_enrichment={"headline": "Sales leader"},
        profile_enriched_at=enriched_at,
        last_contacted_at=now,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    service = _FakeContactReadService(
        rows=[
            {
                "contact": contact,
                "deal_count": 3,
                "source": "hubspot",
                "company": {
                    "id": company_id,
                    "name": "Example Corp",
                    "domain": "example.com",
                },
                "owner": {
                    "id": owner_id,
                    "name": "Nikhil Sharma",
                    "email": "nikhil@example.com",
                },
            }
        ],
        total_count=1,
    )

    result = await list_contacts(
        response=FastAPIResponse(),
        page=1,
        limit=25,
        q=None,
        company_id=None,
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    item = result.data.items[0]  # type: ignore[union-attr]
    dumped = item.model_dump()
    assert dumped == {
        "contact_id": contact_id,
        "full_name": "Jane Doe",
        "email": "jane@example.com",
        "additional_emails": ["jane.alt@example.com"],
        "job_title": "VP Sales",
        "phone": "+1 415 555 0100",
        "mobile_phone": "+1 415 555 0101",
        "linkedin_url": "https://linkedin.com/in/janedoe",
        "company": {
            "id": company_id,
            "name": "Example Corp",
            "domain": "example.com",
        },
        "owner": {
            "id": owner_id,
            "name": "Nikhil Sharma",
            "email": "nikhil@example.com",
        },
        "deal_count": 3,
        "last_contacted_at": now,
        "last_activity_at": now,
        "source": "hubspot",
        "is_profile_enriched": True,
        "profile_enriched_at": enriched_at,
        "created_at": now,
        "updated_at": now,
    }
    assert "first_name" not in dumped
    assert "company_id" not in dumped
    assert "owner_name" not in dumped
    assert "lifecycle_stage" not in dumped
    assert "profile_enrichment_source" not in dumped


@pytest.mark.asyncio
async def test_search_contacts_returns_cursor_page() -> None:
    contact_id = uuid4()
    company_id = uuid4()
    service = _FakeContactSearchService(
        rows=[
            {
                "contact_id": contact_id,
                "full_name": "Sam Altman",
                "email": "sam.altman@openai.com",
                "job_title": "CEO",
                "company": {
                    "id": company_id,
                    "name": "OpenAI",
                    "domain": "openai.com",
                },
                "match_type": "exact_email",
            }
        ],
        next_cursor="next-token",
    )

    result = await contacts_handler.search_contacts(
        response=FastAPIResponse(),
        q="sam.altman@openai.com",
        limit=20,
        cursor=None,
        company_id=None,
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    assert service.request == {
        "q": "sam.altman@openai.com",
        "limit": 20,
        "cursor": None,
        "company_id": None,
    }
    assert result.status == "SUCCESS"
    assert result.data is not None
    assert result.data.items[0].contact_id == contact_id
    assert result.data.items[0].match_type == "exact_email"
    assert result.data.next_cursor == "next-token"
    assert result.data.has_more is True


@pytest.mark.asyncio
async def test_search_contact_create_candidates_uses_create_search() -> None:
    service = _FakeContactSearchService(rows=[], next_cursor=None)

    result = await search_contact_create_candidates(
        response=FastAPIResponse(),
        q="sam@openai.com",
        limit=20,
        cursor=None,
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    assert service.request == {
        "q": "sam@openai.com",
        "limit": 20,
        "cursor": None,
    }
    assert result.status == "SUCCESS"


@pytest.mark.asyncio
async def test_get_contact_detail_returns_page_ready_contract() -> None:
    org_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    company_id = uuid4()
    owner_id = uuid4()
    deal_id = uuid4()
    participant_id = uuid4()
    now = datetime(2026, 6, 20, 15, 45, tzinfo=UTC)
    researched_at = datetime(2026, 6, 19, 8, 15, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=integration_id,
        contact_id=contact_id,
        first_name="Vickesh",
        last_name="Shah",
        full_name="Vickesh Shah",
        email="vickesh@lemonedge.com",
        additional_emails=[],
        job_title="Head of Sales",
        phone="+1 415 555 0100",
        mobile_phone="+1 415 555 0101",
        linkedin_url="https://linkedin.com/in/vickesh",
        website="https://lemonedge.com",
        company_id=company_id,
        company_name="LemonEdge",
        owner_id=owner_id,
        owner_name="Nikhil Singhal",
        owner_email="nikhil@example.com",
        contact_research="Vickesh leads regional sales at LemonEdge.",
        last_researched_at=researched_at,
        profile_enrichment={
            "headline": "Sales leader",
            "_raw": {"employment_history": [{"title": "Head of Sales"}]},
            "_metadata": {"payload_hash": "secret-backend-metadata"},
        },
        profile_enrichment_source="apollo",
        profile_enriched_at=researched_at,
        created_at=now,
        updated_at=now,
        synced_at=now,
    )
    deal = CRMDeal(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        name="Vickesh-LemonEdge",
        deal_status=CRMDealStatus.OPEN,
        stage_name="Appointment Scheduled",
        last_activity_at=now,
    )
    participant = CRMDealParticipant(
        organisation_id=org_id,
        integration_id=integration_id,
        deal_id=deal_id,
        participant_id=participant_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        relationship_role="buyer",
    )
    service = _FakeContactDetailService(
        {
            "contact": contact,
            "source": "hubspot",
            "company": {
                "id": company_id,
                "name": "LemonEdge",
                "domain": "lemonedge.com",
                "website": "https://lemonedge.com",
                "industry": "Financial Services",
                "linkedin_url": None,
                "country": None,
            },
            "owner": {
                "id": owner_id,
                "name": "Nikhil Singhal",
                "email": "nikhil@example.com",
            },
            "deals": [
                {
                    "deal": deal,
                    "participant": participant,
                }
            ],
        }
    )

    result = await get_contact_detail(
        contact_id=contact_id,
        response=FastAPIResponse(),
        background_tasks=BackgroundTasks(),
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    dumped = result.data.model_dump()  # type: ignore[union-attr]
    detail_fields = dumped["detail_field_definitions"]
    assert detail_fields[0] == {
        "key": "full_name",
        "label": "Name",
        "data_type": "string",
        "order": 1,
        "section": "contact",
        "filter_control": "text",
        "editable": True,
        "patch_field": "full_name",
        "edit_control": "text",
        "edit_policy": "always",
        "locked_reason": None,
    }
    fields_by_key = {field["key"]: field for field in detail_fields}
    assert "research_summary.summary" not in fields_by_key
    assert "research_summary.last_researched_at" not in fields_by_key
    assert "profile_enrichment.is_enriched" not in fields_by_key
    assert "profile_enrichment.source" not in fields_by_key
    assert "profile_enrichment.enriched_at" not in fields_by_key
    assert "profile_enrichment.data" not in fields_by_key
    assert fields_by_key["email"] == {
        "key": "email",
        "label": "Email",
        "data_type": "email",
        "order": 3,
        "section": "contact",
        "filter_control": "email",
        "editable": False,
        "patch_field": "email",
        "edit_control": "email",
        "edit_policy": "set_if_empty",
        "locked_reason": "Existing email is used for dedupe and activity matching.",
    }
    assert fields_by_key["linkedin_url"] == {
        "key": "linkedin_url",
        "label": "LinkedIn",
        "data_type": "url",
        "order": 7,
        "section": "contact",
        "filter_control": "link",
        "editable": False,
        "patch_field": "linkedin_url",
        "edit_control": "url",
        "edit_policy": "set_if_empty",
        "locked_reason": "Existing LinkedIn URL is treated as verified profile identity.",
    }
    assert fields_by_key["company.id"] == {
        "key": "company.id",
        "label": "Account ID",
        "data_type": "uuid",
        "order": 13,
        "section": "account",
        "filter_control": "link",
        "editable": False,
        "patch_field": "company_id",
        "edit_control": "account_picker",
        "edit_policy": "set_once",
        "locked_reason": "Account link is set once and cannot be changed from contact detail.",
    }
    assert [field["key"] for field in detail_fields[:5]] == [
        "full_name",
        "job_title",
        "email",
        "additional_emails",
        "phone",
    ]
    assert "deal_count" not in fields_by_key
    assert "pipeline_value" not in fields_by_key
    assert [field["order"] for field in detail_fields] == sorted(
        field["order"] for field in detail_fields
    )
    assert dumped["company"] == {
        "id": company_id,
        "name": "LemonEdge",
        "domain": "lemonedge.com",
        "website": "https://lemonedge.com",
        "industry": "Financial Services",
        "linkedin_url": None,
        "country": None,
    }
    assert dumped["owner"] == {
        "id": owner_id,
        "name": "Nikhil Singhal",
        "email": "nikhil@example.com",
    }
    assert dumped["research_summary"] == {
        "summary": "Vickesh leads regional sales at LemonEdge.",
        "last_researched_at": researched_at,
    }
    assert dumped["profile_enrichment"] == {
        "is_enriched": True,
        "source": "apollo",
        "enriched_at": researched_at,
        "data": {"headline": "Sales leader"},
    }
    assert dumped["deal_involvement"] == [
        {
            "deal_id": deal_id,
            "deal_name": "Vickesh-LemonEdge",
            "deal_status": "open",
            "stage_name": "Appointment Scheduled",
            "last_activity_at": now,
            "participant_id": participant_id,
            "relationship_role": "buyer",
        }
    ]
    assert "stakeholder_summary" not in dumped
    assert "stance" not in dumped["deal_involvement"][0]
    assert "decision_power" not in dumped["deal_involvement"][0]


@pytest.mark.asyncio
async def test_get_contact_detail_allows_safe_missing_identity_fields_to_be_set() -> None:
    org_id = uuid4()
    contact_id = uuid4()
    now = datetime(2026, 6, 20, 15, 45, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        full_name="Unknown Buyer",
        email=None,
        linkedin_url=None,
        company_id=None,
        created_at=now,
        updated_at=now,
        synced_at=now,
    )
    service = _FakeContactDetailService(
        {
            "contact": contact,
            "source": "manual",
            "company": None,
            "owner": None,
            "deals": [],
        }
    )

    result = await get_contact_detail(
        contact_id=contact_id,
        response=FastAPIResponse(),
        background_tasks=BackgroundTasks(),
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    fields_by_key = {
        field["key"]: field
        for field in result.data.model_dump()["detail_field_definitions"]  # type: ignore[union-attr]
    }
    assert fields_by_key["email"]["editable"] is True
    assert fields_by_key["email"]["locked_reason"] is None
    assert fields_by_key["linkedin_url"]["editable"] is True
    assert fields_by_key["linkedin_url"]["locked_reason"] is None
    assert fields_by_key["company.id"]["editable"] is False
    assert fields_by_key["company.id"]["locked_reason"] == "Add an email before linking an account."


@pytest.mark.asyncio
async def test_get_contact_detail_recovers_eligible_contact_without_a_job(monkeypatch) -> None:
    """Viewing a legacy eligible contact should self-heal a missing research dispatch."""
    org_id = uuid4()
    contact_id = uuid4()
    now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@acme.com",
        created_at=now,
        updated_at=now,
        synced_at=now,
    )
    service = _FakeContactDetailService(
        {
            "contact": contact,
            "research_status": "not_started",
            "source": "manual",
            "company": None,
            "owner": None,
            "deals": [],
        }
    )
    calls: list[dict] = []

    async def fake_dispatch(**kwargs):
        """Capture the self-healing dispatch after the detail response is ready."""
        calls.append(kwargs)

    monkeypatch.setattr(contacts_handler, "dispatch_contact_research_async", fake_dispatch)
    background_tasks = BackgroundTasks()

    result = await get_contact_detail(
        contact_id=contact_id,
        response=FastAPIResponse(),
        background_tasks=background_tasks,
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    assert result.data.research_status == "not_started"  # type: ignore[union-attr]
    assert calls == []
    await background_tasks()
    assert calls == [
        {
            "organisation_id": org_id,
            "contact_id": contact_id,
            "trigger": "contact_detail_opened",
        }
    ]


@pytest.mark.asyncio
async def test_delete_contact_handler_returns_archive_impact() -> None:
    org_id = uuid4()
    user_id = uuid4()
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="wrong@example.com",
        full_name="Wrong Contact",
        created_at=now,
        updated_at=now,
    )
    service = _FakeContactWriteService(contact)

    result = await delete_contact(
        contact_id=contact.contact_id,
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.message == "Contact archived and detached from linked records."
    assert result.data.action == "archived_and_detached"  # type: ignore[union-attr]
    assert result.data.contact.contact_id == contact.contact_id  # type: ignore[union-attr]
    assert result.data.impact.cleared_deal_primary_contacts == 1  # type: ignore[union-attr]
    assert service.org_session.commit.count == 1
    assert service.calls[0]["contact_id"] == contact.contact_id


@pytest.mark.asyncio
async def test_delete_contact_handler_returns_guided_provider_conflict() -> None:
    org_id = uuid4()
    user_id = uuid4()

    class ConflictService(_FakeContactWriteService):
        async def delete_contact(self, contact_id, **kwargs):
            raise ContactWriteServiceError(
                "This contact belongs to an external CRM and cannot be archived here.",
                status_code=409,
                data={
                    "code": "external_crm_contact_read_only",
                    "blocked_reason": "Archive it in the source CRM.",
                    "suggested_actions": ["Open the source CRM record."],
                },
            )

    response = FastAPIResponse()
    service = ConflictService(
        CRMContact(
            organisation_id=org_id,
            integration_id=uuid4(),
            contact_id=uuid4(),
            created_at=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
            updated_at=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
        )
    )

    result = await delete_contact(
        contact_id=service.contact.contact_id,
        response=response,
        service=service,  # type: ignore[arg-type]
        user_ids=SimpleNamespace(user_id=user_id, organisation_id=org_id),
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert response.status_code == 409
    assert result.status == "ERROR"
    assert result.data["code"] == "external_crm_contact_read_only"  # type: ignore[index]
    assert service.org_session.rollback.count == 1
